from __future__ import annotations

from datetime import UTC, datetime

import pytest

from playslot.clock import FrozenClock
from playslot.db import create_all, create_db_engine, session_factory, unit_of_work
from playslot.engine.session_engine import SessionEngine
from playslot.enums import UnitType
from playslot.models import Pricing, Unit
from playslot.money import rupees

VENUE = "venue-pune-01"


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 8, 6, 17, 0, tzinfo=UTC))


@pytest.fixture
def factory():
    # A file-backed temp database would exercise the SQLite pragmas, but in-memory keeps
    # the suite fast; the pragmas are covered separately in test_db.py.
    engine = create_db_engine("sqlite://")
    create_all(engine)
    return session_factory(engine)


@pytest.fixture
def seeded(factory, clock):
    """One PC and one PS5, each with pricing already in effect."""
    with unit_of_work(factory) as db:
        db.add_all(
            [
                Unit(
                    id="unit-pc-01",
                    venue_id=VENUE,
                    name="Nova",
                    type=UnitType.PC,
                    zone="Battle Zone",
                ),
                Unit(
                    id="unit-ps5-01",
                    venue_id=VENUE,
                    name="Console 1",
                    type=UnitType.PS5,
                    zone="Console Bay",
                    relay_address="192.168.1.50",
                ),
                Pricing(
                    venue_id=VENUE,
                    unit_type=UnitType.PC,
                    hourly_rate_paise=rupees(120),
                    overtime_rate_paise_per_minute=rupees(5),
                    effective_from=datetime(2026, 1, 1, tzinfo=UTC),
                ),
                Pricing(
                    venue_id=VENUE,
                    unit_type=UnitType.PS5,
                    hourly_rate_paise=rupees(180),
                    overtime_rate_paise_per_minute=rupees(6),
                    controller_surcharge_paise_per_hour=rupees(40),
                    effective_from=datetime(2026, 1, 1, tzinfo=UTC),
                ),
            ]
        )

    return factory


@pytest.fixture
def pool_table(seeded):
    """A snooker table, added on top of the PC and PS5.

    Kept out of ``seeded`` rather than folded into it, so that every existing test keeps
    running against exactly the floor it was written for.
    """
    with unit_of_work(seeded) as db:
        db.add_all(
            [
                Unit(
                    id="unit-pool-01",
                    venue_id=VENUE,
                    name="Table 1",
                    type=UnitType.SNOOKER,
                    zone="Upstairs",
                ),
                Pricing(
                    venue_id=VENUE,
                    unit_type=UnitType.SNOOKER,
                    hourly_rate_paise=rupees(200),
                    overtime_rate_paise_per_minute=rupees(4),
                    effective_from=datetime(2026, 1, 1, tzinfo=UTC),
                ),
            ]
        )

    return seeded


@pytest.fixture
def commands() -> list[tuple[str, bool]]:
    """Captures what the engine would send to the agents."""
    return []


@pytest.fixture
def engine(seeded, clock, commands) -> SessionEngine:
    async def sink(unit_id: str, lock: bool) -> None:
        commands.append((unit_id, lock))

    return SessionEngine(
        seeded, venue_id=VENUE, clock=clock, command_sink=sink
    )
