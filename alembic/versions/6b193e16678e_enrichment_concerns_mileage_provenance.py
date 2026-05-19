"""Add llm_concerns + mileage_is_verified columns to auction_lots.

llm_concerns holds the advisory free-text Concern list the description
enricher produces; it is non-null with a '[]' default so pre-existing rows
read cleanly before re-enrichment. mileage_is_verified is nullable on
purpose: NULL means the listing said nothing about odometer provenance.

Revision ID: 6b193e16678e
Revises: a9cef3ed161c
Create Date: 2026-05-19
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "6b193e16678e"
down_revision: str | Sequence[str] | None = "a9cef3ed161c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "auction_lots",
        sa.Column(
            "llm_concerns",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "auction_lots",
        sa.Column("mileage_is_verified", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("auction_lots", "mileage_is_verified")
    op.drop_column("auction_lots", "llm_concerns")
