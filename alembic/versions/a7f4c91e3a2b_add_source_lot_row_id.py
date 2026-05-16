"""Add source_lot_row_id to auction_lots

HiBid's GraphQL exposes two ids per lot: a stable ``itemId`` (vehicle
identity across re-listings) and a per-listing row ``id``. Our
``source_lot_id`` column has always stored ``itemId`` and is used as
the upsert key — re-listings of the same vehicle correctly map to the
same row.

The bid_poller uses HiBid's ``eventItemIds`` filter for the
single-lot-by-id fetch, and that filter matches against the row ``id``,
NOT ``itemId``. Mismatch produced empty results, the poller interpreted
that as "lot missing from source," and mass-closed 245 lots that were
actually still upcoming. This column stores the row id so the bid_poller
has a working lookup key without breaking upsert semantics.

Nullable on purpose: existing rows get NULL until the next ingest
refreshes them with the row id captured from the parser.

Revision ID: a7f4c91e3a2b
Revises: c3a1b4d27fee
Create Date: 2026-05-16 18:30:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a7f4c91e3a2b"
down_revision: Union[str, Sequence[str], None] = "c3a1b4d27fee"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "auction_lots",
        sa.Column("source_lot_row_id", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("auction_lots", "source_lot_row_id")
