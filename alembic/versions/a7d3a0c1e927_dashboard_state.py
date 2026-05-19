"""dashboard_state singleton for last-visited tracking

The Today inbox surfaces "alerts since your last visit" — new lots matching
watched make/model, state transitions on interested lots, late-discovered
showstoppers. That requires a server-side timestamp the route can read on
load (computing the diff) and then bump (resetting the window).

Single-user app, so this is one row, forever. The CHECK constraint on the
primary key enforces the invariant at the DB layer — a stray INSERT cannot
create a second row. Seeded with `last_visited_at = now()` so the first
page load shows a sensible empty-alerts state rather than "everything ever
ingested is new."

Revision ID: a7d3a0c1e927
Revises: f6c2e9b81a04
Create Date: 2026-05-19 12:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7d3a0c1e927"
down_revision: str | Sequence[str] | None = "f6c2e9b81a04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dashboard_state",
        sa.Column("id", sa.SmallInteger, primary_key=True),
        sa.Column(
            "last_visited_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("id = 1", name="ck_dashboard_state_singleton"),
    )
    op.execute("INSERT INTO dashboard_state (id) VALUES (1)")


def downgrade() -> None:
    op.drop_table("dashboard_state")
