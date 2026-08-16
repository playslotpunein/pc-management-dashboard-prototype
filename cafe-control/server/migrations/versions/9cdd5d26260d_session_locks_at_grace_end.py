"""session locks at grace end

Records, per session, whether the unit it runs on actually shuts down when grace expires.
Billing needs it: a unit that locks can never accrue billable overtime, because the minute
overtime would begin is the minute the machine locks, and everything after that is time
the customer was shut out of.

Two corrections to what autogenerate produced.

**A server default.** NOT NULL with no default fails on the first existing row, and every
venue running this has a table full of them.

**A backfill from the unit.** Defaulting every historic session to false would say that
no PC ever locked, which is the opposite of the truth and would make a re-billed old
session charge for hours a machine spent locked. Existing rows take the value from the
unit they ran on.

Revision ID: 9cdd5d26260d
Revises: 956ca77019d6
Create Date: 2026-08-16 11:04:18.221904

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '9cdd5d26260d'
down_revision: Union[str, Sequence[str], None] = '956ca77019d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "locks_at_grace_end",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )

    # Take it from the unit each session actually ran on, rather than assuming.
    op.execute(
        "UPDATE sessions SET locks_at_grace_end = 1 "
        "WHERE unit_id IN (SELECT id FROM units WHERE enforcement = 'software')"
    )

    # Dropped again so the column is never filled in silently. The engine sets it from
    # the unit at session start; a row that arrived without one is a bug worth surfacing
    # rather than defaulting to "this unit does not lock", which bills for locked time.
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.alter_column("locks_at_grace_end", server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.drop_column("locks_at_grace_end")
