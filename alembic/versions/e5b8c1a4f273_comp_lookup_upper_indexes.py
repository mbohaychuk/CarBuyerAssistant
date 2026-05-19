"""Functional indexes on UPPER(make), UPPER(model), year for comp lookup

scoring.comps.build_comp_set filters both ``historical_sales`` and
``auction_lots`` with ``func.upper(make) == ...`` / ``func.upper(model) == ...``
so a Toyota seeded as ``"toyota"`` matches a lot enriched as ``"Toyota"``.
The existing btree indexes on raw ``make`` / ``model`` are unusable under the
UPPER() wrap; today's small comp set (~500 rows) still completes sub-200ms
on a seq scan, but at projected 5x source count + 6 months of distilled
history (~250k rows in ``historical_sales``) per-comp lookups would creep
into the hundreds of ms and risk hitting the 30s statement_timeout on the
valuator.

The trailing ``year`` column matches the .between() filter that the planner
applies next and lets us serve the entire predicate set from the index without
visiting the heap. Mileage is intentionally NOT in the index — its .between()
window varies per-query and is already cheap once make/model/year have
narrowed the candidate set to a handful of rows.

Revision ID: e5b8c1a4f273
Revises: d4f7a92e1c83
Create Date: 2026-05-19 09:00:00
"""
from collections.abc import Sequence

from alembic import op

revision: str = "e5b8c1a4f273"
down_revision: str | Sequence[str] | None = "d4f7a92e1c83"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # CONCURRENTLY so we don't take ACCESS EXCLUSIVE on either table during
    # the build — at projected ~250k-row scale the non-concurrent variant
    # would stall every writer (valuator, enricher, bid_poller) for seconds.
    # CIC can't run inside a transaction, hence the autocommit_block().
    # Trade-off: an interrupted CIC leaves an INVALID index; rerunning the
    # migration after dropping it manually is the recovery path.
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_historical_sales_make_model_year_upper "
            "ON historical_sales (upper(make), upper(model), year)",
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_auction_lots_make_model_year_upper "
            "ON auction_lots (upper(make), upper(model), year)",
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS "
            "ix_auction_lots_make_model_year_upper",
        )
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS "
            "ix_historical_sales_make_model_year_upper",
        )
