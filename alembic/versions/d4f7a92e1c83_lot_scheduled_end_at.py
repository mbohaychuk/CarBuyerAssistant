"""Add scheduled_end_at to auction_lots

HiBid puts auction-event timing on the auction row (one close time covers
all lots in the event). McDougall puts close times on individual lots
within an auction-event (different lots close at different times). The
existing auction_lots table had no place for per-lot end times, so
McDougall lot end times captured by the parser were silently dropped at
upsert time -- which in turn left bid_poller blind to McDougall lots
because it sorts polling priority by auction.scheduled_end_at NULLS LAST
and McDougall auctions are all NULL.

Nullable + indexed. HiBid lots leave it NULL and the poller's coalesce
falls back to auction.scheduled_end_at. The next McDougall ingester run
populates the column for existing rows via upsert refresh.

Revision ID: d4f7a92e1c83
Revises: b8e2f5c6d419
Create Date: 2026-05-18 16:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4f7a92e1c83"
down_revision: str | Sequence[str] | None = "b8e2f5c6d419"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "auction_lots",
        sa.Column(
            "scheduled_end_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_auction_lots_scheduled_end_at",
        "auction_lots",
        ["scheduled_end_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_auction_lots_scheduled_end_at", table_name="auction_lots")
    op.drop_column("auction_lots", "scheduled_end_at")
