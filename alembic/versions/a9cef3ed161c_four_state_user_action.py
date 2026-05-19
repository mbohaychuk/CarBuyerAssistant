"""Replace 3-value user_action with 4-state workflow.

Adds max_bid_cad / bid_placed_at / won_at columns to auction_lots,
creates lot_action_history audit table, remaps existing user_action
values (maybe → interested, not_interested → passed), promotes
was_purchased_by_us=TRUE rows to user_action='purchased', drops
was_purchased_by_us from auction_lots, and adds bidirectional CHECK
constraints binding the three new columns to user_action states.

ORDERING INVARIANT: steps 5-7 (enum remaps) MUST run before step 8
(was_purchased_by_us → purchased promotion). A row with both
was_purchased_by_us=TRUE AND user_action='not_interested' lands at
'purchased', NOT 'passed' — purchased wins. Tested in
tests/db/test_migration_four_state.py.

Downgrade is LOSSY: formerly-`maybe` rows stay `interested` post-roundtrip;
`bid_placed` rows are remapped to `interested` (bid amount on those rows lives
only in `lot_action_history`, which is also dropped — so the bid commitment is
fully lost on downgrade).

Revision ID: a9cef3ed161c
Revises: a7d3a0c1e927
Create Date: 2026-05-19
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a9cef3ed161c"
down_revision: str | Sequence[str] | None = "a7d3a0c1e927"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. lot_action_history audit table
    op.create_table(
        "lot_action_history",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "lot_id",
            sa.BigInteger,
            sa.ForeignKey("auction_lots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_action", sa.String(16)),
        sa.Column("max_bid_cad", sa.Numeric(12, 2)),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("source", sa.String(32), nullable=False),
    )
    op.create_index(
        "ix_lot_action_history_lot_id_changed_at",
        "lot_action_history",
        ["lot_id", "changed_at"],
    )

    # 2. New current-state columns on auction_lots
    op.add_column(
        "auction_lots",
        sa.Column("max_bid_cad", sa.Numeric(12, 2)),
    )
    op.add_column(
        "auction_lots",
        sa.Column("bid_placed_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "auction_lots",
        sa.Column("won_at", sa.DateTime(timezone=True)),
    )

    # 3. Remap enum values. Ordering matters — see ORDERING INVARIANT above.
    op.execute(
        "UPDATE auction_lots SET user_action = 'interested' "
        "WHERE user_action = 'maybe'"
    )
    op.execute(
        "UPDATE auction_lots SET user_action = 'passed' "
        "WHERE user_action = 'not_interested'"
    )
    # 4. Promote was_purchased_by_us rows to user_action='purchased'.
    #    Stamp won_at from updated_at (best available proxy).
    op.execute(
        "UPDATE auction_lots SET "
        "  user_action = 'purchased', "
        "  won_at = COALESCE(updated_at, now()) "
        "WHERE was_purchased_by_us = TRUE"
    )

    # 5. Seed audit history: one row per labeled lot, source='migration'.
    op.execute(
        "INSERT INTO lot_action_history "
        "  (lot_id, user_action, max_bid_cad, changed_at, source) "
        "SELECT id, user_action, NULL, COALESCE(updated_at, now()), 'migration' "
        "FROM auction_lots WHERE user_action IS NOT NULL"
    )

    # 6. Drop the now-redundant was_purchased_by_us column
    op.drop_column("auction_lots", "was_purchased_by_us")

    # 7. Add bidirectional CHECK constraints.
    # COALESCE(user_action::text, '') turns NULL into '' so the equality is
    # always non-NULL — closing the gap where NULL = 'bid_placed' evaluates to
    # NULL (passes the check) instead of FALSE (rejects the row).
    op.create_check_constraint(
        "ck_auction_lots_bid_placed_iff_max_bid",
        "auction_lots",
        "(COALESCE(user_action::text, '') = 'bid_placed') = (max_bid_cad IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_auction_lots_bid_placed_iff_timestamp",
        "auction_lots",
        "(COALESCE(user_action::text, '') = 'bid_placed') = (bid_placed_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_auction_lots_purchased_iff_won_at",
        "auction_lots",
        "(COALESCE(user_action::text, '') = 'purchased') = (won_at IS NOT NULL)",
    )


def downgrade() -> None:
    # NOTE: op.create_check_constraint prepends the table name, so the
    # constraint names in the DB are doubled: ck_auction_lots_ck_auction_lots_*
    op.drop_constraint("ck_auction_lots_ck_auction_lots_purchased_iff_won_at", "auction_lots")
    op.drop_constraint("ck_auction_lots_ck_auction_lots_bid_placed_iff_timestamp", "auction_lots")
    op.drop_constraint("ck_auction_lots_ck_auction_lots_bid_placed_iff_max_bid", "auction_lots")

    op.add_column(
        "auction_lots",
        sa.Column(
            "was_purchased_by_us",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.execute(
        "UPDATE auction_lots SET was_purchased_by_us = TRUE "
        "WHERE user_action = 'purchased'"
    )

    op.execute(
        "UPDATE auction_lots SET user_action = 'not_interested' "
        "WHERE user_action = 'passed'"
    )
    op.execute(
        "UPDATE auction_lots SET user_action = 'interested' "
        "WHERE user_action = 'purchased'"
    )
    # bid_placed is not a valid legacy value; remap to interested (positive
    # intent). The bid amount is lost — it only ever lived in max_bid_cad (being
    # dropped below) and lot_action_history (also dropped below).
    op.execute(
        "UPDATE auction_lots SET user_action = 'interested' "
        "WHERE user_action = 'bid_placed'"
    )

    op.drop_column("auction_lots", "won_at")
    op.drop_column("auction_lots", "bid_placed_at")
    op.drop_column("auction_lots", "max_bid_cad")

    op.drop_index(
        "ix_lot_action_history_lot_id_changed_at",
        table_name="lot_action_history",
    )
    op.drop_table("lot_action_history")
