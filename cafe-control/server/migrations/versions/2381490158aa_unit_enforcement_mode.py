"""unit enforcement mode

Adds Unit.enforcement, which says how a unit's time limit is actually held: an agent
locking the machine, a relay cutting the display, or nothing at all — a pool or snooker
table, where the manager walks over.

Two corrections to what autogenerate produced, both of which matter on a venue that
already has units in this table:

**A server default.** The generated version added a NOT NULL column with no default,
which fails outright the moment there is a single existing row — and every venue running
this has a full floor of them.

**A backfill by type.** Defaulting everything to "software" would tell the engine to
send lock commands to PS5 stations, which have no agent to receive them. Existing rows
are set from what their type implies, matching DEFAULT_ENFORCEMENT in playslot.enums.

Revision ID: 2381490158aa
Revises: 35e0c3c98b36
Create Date: 2026-08-14 19:48:55.691458
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '2381490158aa'
down_revision: Union[str, Sequence[str], None] = '35e0c3c98b36'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("units", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "enforcement",
                sa.String(length=32),
                nullable=False,
                # Required: without it this statement fails on any table that already
                # holds a row, because NOT NULL has nothing to put in the existing ones.
                server_default="software",
            )
        )

    # Backfill from the type. A PS5 has no agent, so leaving it as "software" would have
    # the engine dispatch lock commands nothing can receive.
    op.execute("UPDATE units SET enforcement = 'relay' WHERE type = 'ps5'")
    op.execute("UPDATE units SET enforcement = 'manual' WHERE type IN ('pool', 'snooker')")

    # The default has done its job and is dropped again. Leaving it in the schema would
    # let a row inserted without an enforcement silently become "software" — which on a
    # pool table means the engine arms lock commands for something with nothing to lock.
    # The model derives the right value from the type at insert time instead.
    with op.batch_alter_table("units", schema=None) as batch_op:
        batch_op.alter_column("enforcement", server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("units", schema=None) as batch_op:
        batch_op.drop_column("enforcement")
