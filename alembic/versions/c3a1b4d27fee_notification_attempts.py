"""notification_attempts and last_notification_error

Phase 13 review fix C2: Notifier was writing notification_status=DONE
even when every Discord POST failed (4xx, 429-after-retry, network
blip). This adds the retry-counter columns so transient failures can
leave the row PENDING for re-claim. Mirrors enrichment_attempts /
valuation_attempts.

Revision ID: c3a1b4d27fee
Revises: 1d58e4b5021c
Create Date: 2026-05-13 21:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3a1b4d27fee"
down_revision: Union[str, Sequence[str], None] = "1d58e4b5021c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "auction_lots",
        sa.Column(
            "notification_attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "auction_lots",
        sa.Column("last_notification_error", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("auction_lots", "last_notification_error")
    op.drop_column("auction_lots", "notification_attempts")
