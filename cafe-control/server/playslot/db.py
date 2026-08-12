"""Database wiring.

SQLite locally, by the architecture's reasoning: a café counter PC should not be running
a database service that someone can stop, uninstall or fail to restart after a power cut.
The schema mirrors Supabase Postgres closely enough that the same models drive both.

Two SQLite settings below are not optional for this workload.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker

from playslot.models import Base


def _configure_sqlite(dbapi_connection: Any, _record: Any) -> None:
    """Apply the pragmas SQLite needs to behave like the Postgres it mirrors."""
    cursor = dbapi_connection.cursor()

    # Off by default in SQLite. Without it a session can reference a deleted unit and
    # the orphan is only discovered when the dashboard renders a blank card.
    cursor.execute("PRAGMA foreign_keys=ON")

    # Write-ahead logging: readers do not block the writer. The dashboard polls while
    # the session engine writes on every tick, and the default rollback journal turns
    # that into "database is locked" during the busiest part of the evening.
    cursor.execute("PRAGMA journal_mode=WAL")

    # Durable enough to survive a process crash, without an fsync per transaction on a
    # counter PC's consumer SSD. A power cut can lose the last moments of WAL; the
    # nightly backup the architecture calls for is the real answer to that.
    cursor.execute("PRAGMA synchronous=NORMAL")

    # Wait rather than fail immediately if a write is in flight.
    cursor.execute("PRAGMA busy_timeout=5000")

    cursor.close()


def create_db_engine(url: str, *, echo: bool = False) -> Engine:
    connect_args: dict[str, Any] = {}

    kwargs: dict[str, Any] = {}

    if url.startswith("sqlite"):
        # The asyncio session engine touches the connection from the event loop's
        # worker threads; SQLite's default same-thread check would reject that.
        connect_args["check_same_thread"] = False

        # Strip exactly one leading slash so all four spellings resolve correctly:
        # "sqlite://" and "sqlite:///:memory:" are in-memory, while
        # "sqlite:///./data/x.db" is relative and "sqlite:////data/x.db" absolute.
        remainder = url[len("sqlite://") :]
        path = remainder[1:] if remainder.startswith("/") else remainder

        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        else:
            # An in-memory database belongs to the connection that opened it, and the
            # default pool hands a different connection to a different thread — which
            # then finds an empty database with none of the tables. StaticPool shares
            # the single connection, so the schema created at startup is the schema
            # every caller sees.
            from sqlalchemy.pool import StaticPool

            kwargs["poolclass"] = StaticPool

    engine = create_engine(
        url, echo=echo, connect_args=connect_args, future=True, **kwargs
    )

    if url.startswith("sqlite"):
        event.listen(engine, "connect", _configure_sqlite)

    return engine


def create_all(engine: Engine) -> None:
    """Create the schema directly from the models.

    Used by the test suite, where an in-memory database is built and thrown away per
    test and a migration run would be pure overhead. The application uses
    :func:`run_migrations` instead — a venue's database has history that matters.
    """
    Base.metadata.create_all(engine)


def run_migrations(url: str) -> str:
    """Bring the database up to head. Safe to call on every start.

    A café counter PC has nobody to run a migration command on it, so the server does it
    itself at startup. Alembic is idempotent — an up-to-date database is a no-op — which
    makes this the right place for it.

    The branch below is the part that matters. A database created by :func:`create_all`
    before migrations existed has all seven tables and no ``alembic_version``. Running an
    upgrade against it would try to CREATE TABLE over tables that already hold the
    venue's sales and fail on the first statement. Stamping records that it is already at
    head without touching the data, which is how an existing install is adopted rather
    than broken.

    Returns the revision the database ended up at.
    """
    from alembic import command
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import inspect

    root = Path(__file__).resolve().parent.parent
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", url)

    engine = create_db_engine(url)

    try:
        with engine.connect() as connection:
            tables = set(inspect(connection).get_table_names())

            if tables and "alembic_version" not in tables:
                logging.getLogger(__name__).info(
                    "Adopting an existing database created before migrations; stamping head"
                )
                command.stamp(config, "head")
                return "stamped"

        command.upgrade(config, "head")

        with engine.connect() as connection:
            revision = MigrationContext.configure(connection).get_current_revision()

        return revision or "base"
    finally:
        engine.dispose()


def session_factory(engine: Engine) -> sessionmaker[OrmSession]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def unit_of_work(factory: sessionmaker[OrmSession]) -> Iterator[OrmSession]:
    """Transaction scope: commit on success, roll back on any exception.

    Used by the engine tick so that a state change, its activity-log row and its outbox
    entry are one atomic write. A half-applied tick would leave a unit locked with no
    record of why.
    """
    session = factory()

    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
