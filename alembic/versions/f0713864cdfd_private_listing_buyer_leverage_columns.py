"""private_listing buyer-leverage columns

Revision ID: f0713864cdfd
Revises: 2a0ad4b27278
Create Date: 2026-06-30 22:41:00.705474

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f0713864cdfd'
down_revision: Union[str, Sequence[str], None] = '2a0ad4b27278'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the buyer-leverage columns: the first-seen asking price and the count
    of price drops since. Both additive — price_drop_count defaults to 0 so
    existing rows backfill, original_asking_price_cad is nullable (populated on
    the next insert/drop)."""
    op.add_column(
        "private_listing",
        sa.Column("original_asking_price_cad", sa.Numeric(12, 2), nullable=True),
    )
    op.add_column(
        "private_listing",
        sa.Column(
            "price_drop_count", sa.Integer(), server_default=sa.text("0"), nullable=False,
        ),
    )


def downgrade() -> None:
    """Drop the buyer-leverage columns."""
    op.drop_column("private_listing", "price_drop_count")
    op.drop_column("private_listing", "original_asking_price_cad")
