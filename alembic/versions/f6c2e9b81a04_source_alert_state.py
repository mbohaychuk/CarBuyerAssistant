"""source_alert_state for the stale-source watchdog

The source_watchdog app fires hourly, checks each registered source's most
recent auctions.last_seen_at, and posts a Discord alert to the system_health
channel if >24h has elapsed. Without per-source dedup state, that would
generate 24 alerts per stale source per day — enough to train the operator
to mute the channel, which is the opposite of useful.

This table stores last_alerted_at per source so the watchdog can rate-limit
itself to one alert per ~24h window. Rows materialize on first alert.

Revision ID: f6c2e9b81a04
Revises: e5b8c1a4f273
Create Date: 2026-05-19 10:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6c2e9b81a04"
down_revision: str | Sequence[str] | None = "e5b8c1a4f273"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_alert_state",
        sa.Column("source", sa.String(64), primary_key=True),
        sa.Column(
            "last_alerted_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("source_alert_state")
