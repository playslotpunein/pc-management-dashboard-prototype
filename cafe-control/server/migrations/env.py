"""Alembic environment.

Two settings here are load-bearing rather than boilerplate.

**``render_as_batch=True``.** SQLite cannot ALTER a column — it has no DROP COLUMN worth
the name, no type change, no constraint change. Batch mode makes Alembic emit the only
thing SQLite understands: create a new table, copy the rows, drop the old one, rename.
Without it roughly half of all future migrations would generate fine and then fail on the
venue's database, which is exactly the database with the real money in it.

**The URL comes from settings, not alembic.ini.** The venue's database path is set by the
same configuration the app reads, so `alembic upgrade head` and the running server can
never disagree about which file they are migrating.

The metadata is the app's own models. That is the point the architecture makes about
defining the schema once: these migrations are generated from the same classes that drive
local SQLite and Supabase Postgres, so the two cannot drift.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from playslot.config import settings
from playslot.db import create_db_engine
from playslot.models import Base, EnumValue, UtcDateTime

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# alembic.ini deliberately leaves the URL empty, so the CLI falls back to the same
# settings the server reads and the two can never disagree about which file they mean.
#
# A URL already on the config wins, though: run_migrations() sets one explicitly, and
# overwriting it here would silently migrate the default database instead of the one the
# caller asked for — which in a test looks like the migration doing nothing at all.
if not config.get_main_option("sqlalchemy.url", None):
    config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def render_item(type_, obj, autogen_context) -> str | bool:
    """Render the app's TypeDecorators as the column type they actually create.

    A migration describes the *database*. ``UtcDateTime`` and ``EnumValue`` are
    Python-side conveniences — one normalises timezones on the way in and out, the other
    coerces a StrEnum — and both emit ordinary DATETIME and VARCHAR columns. Rendering
    them by their dotted path is worse than useless: Alembic writes
    ``EnumValue(length=32)`` without the ``enum_class`` the constructor requires, so the
    migration fails at runtime *after* the statements before it have already applied,
    leaving a half-built schema.

    Returning the underlying type also keeps generated migrations readable by anyone who
    knows SQLAlchemy but not this codebase.
    """
    if type_ != "type":
        return False

    if isinstance(obj, UtcDateTime):
        return "sa.DateTime(timezone=True)"

    if isinstance(obj, EnumValue):
        length = getattr(obj.impl, "length", None) or getattr(obj, "length", 32)
        return f"sa.String(length={length})"

    return False


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting. Useful for reviewing a migration."""
    url = config.get_main_option("sqlalchemy.url")

    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # compare_type catches a column whose type changed in the models but not in the
        # database — silent on a money column until a value no longer fits.
        compare_type=True,
        compare_server_default=True,
        render_as_batch=_is_sqlite(url or ""),
        render_item=render_item,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # The app's own engine factory rather than engine_from_config: it creates the
    # database's parent directory and applies the SQLite pragmas. Without the first,
    # `alembic upgrade head` on a fresh install fails on a missing folder — which is
    # precisely the machine where a migration has to work unattended.
    connectable = create_db_engine(config.get_main_option("sqlalchemy.url"))

    with connectable.connect() as connection:
        sqlite = connection.dialect.name == "sqlite"

        # The other half of render_as_batch, and the half that only shows up on a
        # database with real data in it.
        #
        # Batch mode rebuilds a table by dropping it and renaming a copy into place.
        # `sessions` and `sales` carry foreign keys into `units`, so on SQLite — which
        # enforces those — the DROP fails with "FOREIGN KEY constraint failed" and the
        # migration dies half way. It does not fail on an empty table, so a migration
        # can pass every test and then break on the one database that matters.
        #
        # The keys are re-enabled below. This must sit outside begin_transaction():
        # SQLite ignores PRAGMA foreign_keys inside a transaction, which would leave the
        # setting untouched and the failure exactly as it was.
        #
        # The commit closes the implicit transaction SQLAlchemy 2.0 opens around any
        # statement, so Alembic starts from a clean connection and owns its own
        # transaction rather than inheriting one. Belt and braces: migrations here run
        # unattended on a counter PC at startup, where the failure would surface as a
        # venue whose floor does not load.
        if sqlite:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            render_as_batch=sqlite,
            render_item=render_item,
        )

        try:
            with context.begin_transaction():
                context.run_migrations()
        finally:
            if sqlite:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
