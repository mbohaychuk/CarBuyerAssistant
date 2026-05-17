"""Add buyer_premium_max_cad + buyer_premium_min_cad to auctions

McDougall states "15% Buyer Premium to a Max $2000 per lot and a Minimum
of $20", which our existing single ``buyer_premium_pct`` column cannot
represent. With only a flat percent we would overstate ``all_in_cost``
on high bids (cap not applied) and understate it on very low bids (floor
not applied), silently biasing ``price_deal_score`` and dropping real
deals from the notifier on the high end.

Both columns are nullable: HiBid and any future linear-premium source
sets them to NULL and the scoring math collapses to the prior formula.
McDougall sets max=2000 and min=20.

Revision ID: b8e2f5c6d419
Revises: a7f4c91e3a2b
Create Date: 2026-05-16 21:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b8e2f5c6d419"
down_revision: str | Sequence[str] | None = "a7f4c91e3a2b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "auctions",
        sa.Column("buyer_premium_max_cad", sa.Numeric(10, 2), nullable=True),
    )
    op.add_column(
        "auctions",
        sa.Column("buyer_premium_min_cad", sa.Numeric(10, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("auctions", "buyer_premium_min_cad")
    op.drop_column("auctions", "buyer_premium_max_cad")
