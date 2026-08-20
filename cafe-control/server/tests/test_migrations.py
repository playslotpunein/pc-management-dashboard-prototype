"""Migration tests.

The schema is defined once in models.py and the migrations are generated from it. These
assert that the two have not drifted, and that the upgrade path works on the two
databases that actually exist in the wild: a fresh install, and one created before
migrations existed.
"""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from playslot.db import create_all, create_db_engine, run_migrations
from playslot.enums import EnforcementMode, PaymentMethod, SessionSource, UnitType
from playslot.models import Sale, Session as SessionRow, Unit


def _config(url: str):
    from pathlib import Path

    from alembic.config import Config

    import playslot

    root = Path(playslot.__file__).resolve().parent.parent
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", url)

    return config


def upgrade_to(url: str, revision: str) -> None:
    """Migrate to one specific revision, to stage a database as it was at that point."""
    from alembic import command

    command.upgrade(_config(url), revision)


def downgrade_to(url: str, revision: str) -> None:
    from alembic import command

    command.downgrade(_config(url), revision)

EXPECTED_TABLES = {
    "units",
    "sessions",
    "sales",
    "pricing",
    "agents",
    "sync_outbox",
    "activity_log",
    "inventory",
}


@pytest.fixture
def db_path(tmp_path):
    return f"sqlite:///{tmp_path / 'venue.db'}"


def table_names(url: str) -> set[str]:
    engine = create_db_engine(url)

    try:
        with engine.connect() as connection:
            return set(inspect(connection).get_table_names())
    finally:
        engine.dispose()


class TestFreshInstall:
    def test_upgrading_an_empty_database_creates_the_seven_tables(self, db_path):
        run_migrations(db_path)

        assert EXPECTED_TABLES <= table_names(db_path)

    def test_the_revision_is_recorded(self, db_path):
        revision = run_migrations(db_path)

        assert revision not in ("", "base", None)
        assert "alembic_version" in table_names(db_path)

    def test_running_twice_is_a_no_op(self, db_path):
        """The server migrates on every start, so this happens on every restart."""
        first = run_migrations(db_path)
        second = run_migrations(db_path)

        assert first == second
        assert EXPECTED_TABLES <= table_names(db_path)


class TestAdoptingAnExistingDatabase:
    def test_a_database_built_by_create_all_is_stamped_not_upgraded(self, db_path):
        """The upgrade path for anyone already running the pre-migration build.

        Their database has all seven tables and no alembic_version. An upgrade would
        try to CREATE TABLE over tables holding real sales and fail on the first
        statement, so it is stamped instead.
        """
        engine = create_db_engine(db_path)
        create_all(engine)
        engine.dispose()

        assert "alembic_version" not in table_names(db_path)

        result = run_migrations(db_path)

        assert result == "stamped"
        assert "alembic_version" in table_names(db_path)
        assert EXPECTED_TABLES <= table_names(db_path)

    def test_adoption_does_not_touch_existing_rows(self, tmp_path):
        """The whole point: a venue's sales survive the upgrade."""
        url = f"sqlite:///{tmp_path / 'venue.db'}"

        engine = create_db_engine(url)
        create_all(engine)

        # Written through the model rather than as literal SQL. A hand-written INSERT
        # here has to be edited every time a column is added, and it fails in a way that
        # looks like the migration broke when in fact only the test went stale.
        with Session(engine) as session:
            session.add(Unit(id="u1", venue_id="v1", name="Nova", type=UnitType.PC))
            session.commit()

        engine.dispose()

        run_migrations(url)

        raw = sqlite3.connect(tmp_path / "venue.db")
        rows = list(raw.execute("SELECT id, name FROM units"))
        raw.close()

        assert rows == [("u1", "Nova")]

    def test_a_stamped_database_then_migrates_normally(self, db_path):
        engine = create_db_engine(db_path)
        create_all(engine)
        engine.dispose()

        run_migrations(db_path)

        # A later start finds alembic_version present and takes the ordinary path.
        assert run_migrations(db_path) != "stamped"


class TestMigratingATableOtherTablesPointAt:
    """The case an empty test database cannot reach.

    Batch mode rebuilds a table by dropping it and renaming a copy in. `sessions` and
    `sales` hold foreign keys into `units`, so on SQLite the DROP fails with "FOREIGN KEY
    constraint failed" — but only once those rows exist. Every migration test that seeds
    units alone passes straight through it.
    """

    def test_a_migration_survives_referencing_rows(self, tmp_path):
        """Rebuild a referenced table while rows point at it.

        Migrating a *fresh* database never rebuilds anything — it creates the tables — so
        seeding one and re-running head is a no-op that passes whatever the pragma does.
        The rebuild has to be exercised on a populated database.

        Stepping back one revision and forward again does that, and does it without
        naming a revision: pinning to a specific one means the models outgrow the staged
        schema on the next column added, and the test fails for a reason that has nothing
        to do with what it checks.
        """
        url = f"sqlite:///{tmp_path / 'venue.db'}"

        run_migrations(url)

        engine = create_db_engine(url)

        # A row in each table that points at another, so whichever table a migration
        # rebuilds, something is referencing it: sessions -> units, sales -> sessions.
        # With only one of them present the rebuild has nothing to trip over and the
        # test passes whatever the foreign-key handling does.
        with Session(engine) as session:
            session.add(Unit(id="u1", venue_id="v1", name="PC 1", type=UnitType.PC))
            session.flush()
            session.add(
                SessionRow(
                    id="s1",
                    venue_id="v1",
                    unit_id="u1",
                    rate_snapshot_paise=12000,
                    duration_minutes=60,
                )
            )
            session.flush()
            session.add(
                Sale(
                    id="sale1",
                    venue_id="v1",
                    session_id="s1",
                    source=SessionSource.WALK_IN,
                    amount_paise=12000,
                    payment_method=PaymentMethod.CASH,
                )
            )
            session.commit()

        engine.dispose()

        # Down and back up: both directions rebuild a table something else points at.
        downgrade_to(url, "-1")

        landed = run_migrations(url)

        raw = sqlite3.connect(tmp_path / "venue.db")

        try:
            # The rebuild must not have taken the referencing rows with it.
            assert list(raw.execute("SELECT id FROM sessions")) == [("s1",)]
            assert list(raw.execute("SELECT id FROM units")) == [("u1",)]
            assert list(raw.execute("SELECT id FROM sales")) == [("sale1",)]
            assert list(raw.execute("PRAGMA foreign_key_check")) == []

            # And the new revision has to be *recorded*, not merely executed. If the
            # version stamp rolls back while the DDL sticks, every restart re-runs the
            # migration — survivable here only because this one happens to be idempotent.
            stamped = list(raw.execute("SELECT version_num FROM alembic_version"))

            assert stamped == [(landed,)]
            assert stamped != [("2381490158aa",)]
        finally:
            raw.close()

    def test_a_console_stops_claiming_a_relay_it_never_had(self, tmp_path):
        """'relay' is no longer a value the code can read; a leftover row raises."""
        url = f"sqlite:///{tmp_path / 'venue.db'}"

        upgrade_to(url, "2381490158aa")

        raw = sqlite3.connect(tmp_path / "venue.db")
        raw.execute(
            "INSERT INTO units (id, venue_id, name, type, zone, state, enforcement, notes, "
            "created_at) VALUES ('u1','v1','PS5 1','ps5','Bay','available','relay','',"
            "'2026-08-01 10:00:00')"
        )
        raw.commit()
        raw.close()

        run_migrations(url)

        engine = create_db_engine(url)

        try:
            with Session(engine) as session:
                # Reading it at all is the assertion: an unmapped value raises here.
                assert session.get(Unit, "u1").enforcement is EnforcementMode.MANUAL
        finally:
            engine.dispose()

    def test_foreign_keys_are_on_again_afterwards(self, tmp_path):
        """Migrating turns them off. Leaving them off would silently accept orphans."""
        url = f"sqlite:///{tmp_path / 'venue.db'}"

        run_migrations(url)

        engine = create_db_engine(url)

        try:
            with engine.connect() as connection:
                enabled = list(connection.exec_driver_sql("PRAGMA foreign_keys"))[0][0]
        finally:
            engine.dispose()

        assert enabled == 1


class TestSchemaMatchesModels:
    def test_migrated_schema_has_the_same_tables_as_create_all(self, tmp_path):
        """If these diverge, the migration and the models have drifted.

        `alembic check` is the sharper version of this and is run in CI; this catches
        the coarse case — a table added to models.py with no migration written — without
        needing the alembic CLI.
        """
        migrated_url = f"sqlite:///{tmp_path / 'migrated.db'}"
        direct_url = f"sqlite:///{tmp_path / 'direct.db'}"

        run_migrations(migrated_url)

        engine = create_db_engine(direct_url)
        create_all(engine)
        engine.dispose()

        migrated = table_names(migrated_url) - {"alembic_version"}
        direct = table_names(direct_url)

        assert migrated == direct

    def test_money_columns_survive_as_integers(self, db_path):
        """Paise are integers. A migration that made them REAL would reintroduce drift."""
        run_migrations(db_path)

        engine = create_db_engine(db_path)

        try:
            with engine.connect() as connection:
                columns = {
                    column["name"]: str(column["type"]).upper()
                    for column in inspect(connection).get_columns("sales")
                }
        finally:
            engine.dispose()

        assert "INT" in columns["amount_paise"]
        assert "REAL" not in columns["amount_paise"]
        assert "FLOAT" not in columns["amount_paise"]
