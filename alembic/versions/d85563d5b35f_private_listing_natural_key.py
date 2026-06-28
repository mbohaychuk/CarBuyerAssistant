"""private_listing natural key

Revision ID: d85563d5b35f
Revises: 1d6201a6e2d0
Create Date: 2026-06-28 11:28:45.488690

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd85563d5b35f'
down_revision: Union[str, Sequence[str], None] = '1d6201a6e2d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # private_listing is empty (created empty in the split), so the NOT NULL
    # natural-key columns can be added directly with no backfill.
    op.add_column("private_listing", sa.Column("source", sa.String(length=64), nullable=False))
    op.add_column(
        "private_listing",
        sa.Column("source_listing_id", sa.String(length=128), nullable=False),
    )
    op.create_index("ix_private_listing_source", "private_listing", ["source"])
    op.create_unique_constraint(
        "uq_private_listing_source_listing", "private_listing",
        ["source", "source_listing_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_private_listing_source_listing", "private_listing", type_="unique",
    )
    op.drop_index("ix_private_listing_source", table_name="private_listing")
    op.drop_column("private_listing", "source_listing_id")
    op.drop_column("private_listing", "source")
