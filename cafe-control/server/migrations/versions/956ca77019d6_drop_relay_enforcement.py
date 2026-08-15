"""drop relay enforcement

The venue does not want an automatic hardware cut. A console's time limit is handled by
whoever is at the counter — walking over, or switching the screen off where the room has
that option — which is the same thing a pool table already does. So RELAY goes, and PS5
stations become MANUAL.

Autogenerate saw only the dropped column. The half that matters it cannot see: a venue
that has already run 2381490158aa has PS5 rows holding the string 'relay', and nothing
in the code maps that any more. Left alone, every one of those rows raises ValueError on
the next read — the whole console bay disappears from the dashboard on the first poll
after deploy. The UPDATE below is the actual upgrade; the column drop is tidying.

Revision ID: 956ca77019d6
Revises: 2381490158aa
Create Date: 2026-08-15 10:00:32.009872

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '956ca77019d6'
down_revision: Union[str, Sequence[str], None] = '2381490158aa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Data first. A row left on 'relay' is unreadable the moment the new code loads.
    op.execute("UPDATE units SET enforcement = 'manual' WHERE enforcement = 'relay'")

    with op.batch_alter_table("units", schema=None) as batch_op:
        batch_op.drop_column("relay_address")


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("units", schema=None) as batch_op:
        batch_op.add_column(sa.Column("relay_address", sa.VARCHAR(length=128), nullable=True))

    # Put the consoles back the way the previous revision left them. Any PC or table a
    # venue set to MANUAL by hand stays MANUAL — only the type-derived default is undone,
    # which is all this revision changed.
    op.execute("UPDATE units SET enforcement = 'relay' WHERE type = 'ps5'")
