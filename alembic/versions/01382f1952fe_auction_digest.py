"""auction_digest

Revision ID: 01382f1952fe
Revises: 0ad05f1443a0
Create Date: 2026-05-29 20:31:37.357412

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '01382f1952fe'
down_revision: Union[str, Sequence[str], None] = '0ad05f1443a0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "auctions",
        sa.Column("digest_sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_auctions_digest_eligibility", "auctions", ["scheduled_start_at"],
        unique=False,
        postgresql_where=sa.text("digest_sent_at IS NULL AND scheduled_start_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_auctions_digest_eligibility", table_name="auctions")
    op.drop_column("auctions", "digest_sent_at")
