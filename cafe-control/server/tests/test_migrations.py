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
from playslot.enums import UnitType
from playslot.models import Unit

EXPECTED_TABLES = {
    "units",
    "sessions",
    "sales",
    "pricing",
    "agents",
    "sync_outbox",
    "activity_log",
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
