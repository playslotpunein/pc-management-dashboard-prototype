"""Inventory: the shelf, selling onto a tab, and the low-stock alert.

The load-bearing properties, in order of what would hurt most if wrong:

* **Stock and the tab never disagree.** Adding an item decrements the shelf and appends
  the line in one transaction; a sale that cannot be covered is refused, not allowed to
  drive the count negative.
* **The price is snapshotted.** A price change after a can is rung up does not rewrite the
  open tab, the same rule time follows.
* **The low-stock alert fires on the crossing**, once — not on every can sold below the
  line, and again only when it hits zero.
"""

from __future__ import annotations

import pytest

from playslot.db import unit_of_work
from playslot.engine.session_engine import ItemNotFound, OutOfStock, SessionEngineError
from playslot.enums import AlertKind
from playslot.models import InventoryItem, Sale
from playslot.money import rupees

from .conftest import VENUE
from .test_engine import PC


@pytest.fixture
def coke(engine):
    """A drink priced at ₹60, ten in stock, low at three."""
    return engine.add_inventory_item(
        name="Coke", unit_price_paise=rupees(60), category="Drinks",
        stock_qty=10, low_stock_threshold=3,
    ).id


class TestTheShelf:
    def test_an_item_is_added_and_listed(self, engine):
        engine.add_inventory_item(name="Chips", unit_price_paise=rupees(40))

        names = [i.name for i in engine.list_inventory()]

        assert names == ["Chips"]

    def test_a_restock_raises_the_count(self, engine, coke):
        item = engine.restock(coke, 5)

        assert item.stock_qty == 15

    def test_archiving_hides_it_from_the_shelf_without_deleting(self, engine, coke):
        engine.update_inventory_item(coke, archived=True)

        assert engine.list_inventory() == []
        assert len(engine.list_inventory(include_archived=True)) == 1

    def test_a_price_change_does_not_touch_stock(self, engine, coke):
        engine.update_inventory_item(coke, unit_price_paise=rupees(70))

        item = engine.list_inventory()[0]

        assert item.unit_price_paise == rupees(70)
        assert item.stock_qty == 10


class TestSellingOntoATab:
    async def test_it_decrements_stock_and_bills_the_line(self, engine, coke):
        session_id = engine.start_session(unit_id=PC, duration_minutes=60)

        await engine.add_item_to_session(session_id=session_id, item_id=coke, qty=2)

        assert engine.list_inventory()[0].stock_qty == 8

        bill = engine.preview_bill(session_id)
        item_lines = [line for line in bill.lines if line.kind == "item"]

        assert len(item_lines) == 1
        assert item_lines[0].amount_paise == rupees(120)  # 2 × ₹60
        # ₹120 for the booked hour on the PC, plus the two cokes.
        assert bill.total_paise == rupees(240)

    async def test_it_refuses_to_oversell(self, engine, coke):
        session_id = engine.start_session(unit_id=PC, duration_minutes=60)

        with pytest.raises(OutOfStock, match="Only 10"):
            await engine.add_item_to_session(session_id=session_id, item_id=coke, qty=11)

        # And the shelf is untouched — the refusal is atomic.
        assert engine.list_inventory()[0].stock_qty == 10

    async def test_the_price_is_snapshotted_onto_the_line(self, engine, coke):
        session_id = engine.start_session(unit_id=PC, duration_minutes=60)
        await engine.add_item_to_session(session_id=session_id, item_id=coke, qty=1)

        # Price doubles after the sale; the open tab must not move.
        engine.update_inventory_item(coke, unit_price_paise=rupees(120))

        line = [l for l in engine.preview_bill(session_id).lines if l.kind == "item"][0]

        assert line.amount_paise == rupees(60)

    async def test_removing_a_line_puts_the_stock_back(self, engine, coke):
        session_id = engine.start_session(unit_id=PC, duration_minutes=60)
        line = await engine.add_item_to_session(session_id=session_id, item_id=coke, qty=3)

        assert engine.list_inventory()[0].stock_qty == 7

        engine.remove_item_from_session(session_id=session_id, line_id=line["line_id"])

        assert engine.list_inventory()[0].stock_qty == 10
        assert not [l for l in engine.preview_bill(session_id).lines if l.kind == "item"]

    async def test_the_lines_are_stored_on_the_closed_sale(self, engine, coke):
        session_id = engine.start_session(unit_id=PC, duration_minutes=60)
        await engine.add_item_to_session(session_id=session_id, item_id=coke, qty=2)

        sale = engine.end_session(session_id=session_id)

        with unit_of_work(engine._factory) as db:
            stored = db.get(Sale, sale.id)
            kinds = [line["kind"] for line in stored.lines]

        assert "item" in kinds
        assert sale.amount_paise == rupees(240)

    async def test_a_closed_tab_takes_no_more_items(self, engine, coke):
        session_id = engine.start_session(unit_id=PC, duration_minutes=60)
        engine.end_session(session_id=session_id)

        with pytest.raises(SessionEngineError, match="already"):
            await engine.add_item_to_session(session_id=session_id, item_id=coke, qty=1)

    async def test_an_unknown_item_is_rejected(self, engine):
        session_id = engine.start_session(unit_id=PC, duration_minutes=60)

        with pytest.raises(ItemNotFound):
            await engine.add_item_to_session(
                session_id=session_id, item_id="no-such", qty=1
            )


class TestTheLowStockAlert:
    async def test_it_fires_once_on_crossing_the_threshold(self, engine, coke, published):
        """Ten in stock, low at three: selling down to three alerts; the next can does not."""
        session_id = engine.start_session(unit_id=PC, duration_minutes=60)

        # 10 → 4: still above the line, no alert.
        await engine.add_item_to_session(session_id=session_id, item_id=coke, qty=6)
        assert published == []

        # 4 → 3: crosses, one alert.
        await engine.add_item_to_session(session_id=session_id, item_id=coke, qty=1)
        assert [a.kind for a in published] == [AlertKind.LOW_STOCK]
        assert "3 left" in published[0].message

        # 3 → 2: already low, no second nag.
        await engine.add_item_to_session(session_id=session_id, item_id=coke, qty=1)
        assert len(published) == 1

    async def test_running_out_raises_its_own_alert(self, engine, coke, published):
        session_id = engine.start_session(unit_id=PC, duration_minutes=60)

        await engine.add_item_to_session(session_id=session_id, item_id=coke, qty=7)  # →3 low
        await engine.add_item_to_session(session_id=session_id, item_id=coke, qty=3)  # →0 out

        kinds = [a.kind for a in published]
        messages = [a.message for a in published]

        assert kinds == [AlertKind.LOW_STOCK, AlertKind.LOW_STOCK]
        assert any("out of stock" in m for m in messages)

    async def test_an_item_with_no_threshold_only_alerts_on_empty(self, engine, published):
        """Threshold zero means 'tell me when it runs out', nothing before."""
        water = engine.add_inventory_item(
            name="Water", unit_price_paise=rupees(20), stock_qty=2
        ).id
        session_id = engine.start_session(unit_id=PC, duration_minutes=60)

        await engine.add_item_to_session(session_id=session_id, item_id=water, qty=1)
        assert published == []

        await engine.add_item_to_session(session_id=session_id, item_id=water, qty=1)
        assert [a.kind for a in published] == [AlertKind.LOW_STOCK]
        assert "out of stock" in published[0].message

    async def test_a_low_stock_alert_is_venue_level_not_unit_level(self, engine, coke, published):
        session_id = engine.start_session(unit_id=PC, duration_minutes=60)
        await engine.add_item_to_session(session_id=session_id, item_id=coke, qty=8)

        assert published[0].unit_id == ""
        assert published[0].session_id == ""
        assert published[0].triggers_lock is False
