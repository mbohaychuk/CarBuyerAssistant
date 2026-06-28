"""vehicle_offer split

Splits the monolithic ``auction_lots`` table into a source-agnostic
``vehicle_offer`` parent + an ``auction_lot`` child (shared PK) and adds an
empty ``private_listing`` child, per the supertype/subtype storage decision.

Strategy: RENAME-IN-PLACE. ``auction_lots`` is renamed to ``vehicle_offer`` so
every id, the id sequence, the four partial pending indexes, the make/model
indexes, and all three inbound FK *values* survive untouched on the same
physical rows — NOTIFY/queue keep working throughout. The auction-specific
columns are carved out into the new ``auction_lot`` child; the absence of an
``auction_lot`` row marks an offer as non-auction (the parent ``offer_kind``
discriminator carries it for SQLAlchemy).

Revision ID: 1d6201a6e2d0
Revises: 403d74523f36
Create Date: 2026-06-28 11:14:12.397857

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1d6201a6e2d0'
down_revision: Union[str, Sequence[str], None] = '403d74523f36'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Columns carved out of the parent into the auction_lot child, in copy order.
_CHILD_COLS = [
    "auction_id", "source_lot_id", "source_lot_row_id", "lot_number",
    "scheduled_end_at", "current_high_bid_cad", "last_bid_observed_at",
    "bid_count_visible", "reserve_met", "lot_status", "closed_at",
    "final_bid_cad", "early_warning_notified_at", "cheap_notified_at",
    "closing_notified_at", "trajectory_notified_at", "extended_notified_at",
]

# Surviving parent indexes renamed to the vehicle_offer convention (old, new).
# The composite (price_deal_score, lot_status) index is NOT here — it spans
# parent+child and is decomposed instead.
_PARENT_INDEX_RENAMES = [
    ("ix_auction_lots_enrichment_pending", "ix_vehicle_offer_enrichment_pending"),
    ("ix_auction_lots_enrichment_status", "ix_vehicle_offer_enrichment_status"),
    ("ix_auction_lots_make", "ix_vehicle_offer_make"),
    ("ix_auction_lots_make_model_year", "ix_vehicle_offer_make_model_year"),
    ("ix_auction_lots_make_model_year_upper", "ix_vehicle_offer_make_model_year_upper"),
    ("ix_auction_lots_model", "ix_vehicle_offer_model"),
    ("ix_auction_lots_notification_pending", "ix_vehicle_offer_notification_pending"),
    ("ix_auction_lots_notification_status", "ix_vehicle_offer_notification_status"),
    ("ix_auction_lots_rarity_score", "ix_vehicle_offer_rarity_score"),
    ("ix_auction_lots_user_action", "ix_vehicle_offer_user_action"),
    ("ix_auction_lots_valuation_pending", "ix_vehicle_offer_valuation_pending"),
    ("ix_auction_lots_valuation_status", "ix_vehicle_offer_valuation_status"),
    ("ix_auction_lots_vision_pending", "ix_vehicle_offer_vision_pending"),
    ("ix_auction_lots_vision_status", "ix_vehicle_offer_vision_status"),
    ("ix_auction_lots_was_purchased_by_us", "ix_vehicle_offer_was_purchased_by_us"),
]


def upgrade() -> None:
    # 1. Rename the monolith → parent. Sequence ownership, the pending indexes,
    #    and the make/model/functional indexes ride along on the same rows.
    op.rename_table("auction_lots", "vehicle_offer")
    op.execute("ALTER SEQUENCE auction_lots_id_seq RENAME TO vehicle_offer_id_seq")
    op.execute(
        "ALTER TABLE vehicle_offer RENAME CONSTRAINT pk_auction_lots TO pk_vehicle_offer"
    )

    # 2. Discriminator: add nullable → backfill 'auction' → NOT NULL.
    op.add_column("vehicle_offer", sa.Column("offer_kind", sa.String(length=16), nullable=True))
    op.execute("UPDATE vehicle_offer SET offer_kind = 'auction'")
    op.alter_column("vehicle_offer", "offer_kind", nullable=False)

    # 3. Create the auction child (shared PK → parent).
    op.create_table(
        "auction_lot",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("auction_id", sa.BigInteger(), nullable=False),
        sa.Column("source_lot_id", sa.String(length=128), nullable=False),
        sa.Column("source_lot_row_id", sa.BigInteger(), nullable=True),
        sa.Column("lot_number", sa.String(length=64), nullable=True),
        sa.Column("scheduled_end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_high_bid_cad", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("last_bid_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bid_count_visible", sa.Integer(), nullable=True),
        sa.Column("reserve_met", sa.Boolean(), nullable=True),
        sa.Column("lot_status", sa.String(length=32), server_default="open", nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("final_bid_cad", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("early_warning_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cheap_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closing_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trajectory_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("extended_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_auction_lot"),
        sa.ForeignKeyConstraint(
            ["id"], ["vehicle_offer.id"],
            name="fk_auction_lot_id_vehicle_offer", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["auction_id"], ["auctions.id"],
            name="fk_auction_lot_auction_id_auctions", ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "auction_id", "source_lot_id", name="uq_auction_lot_auction_source_lot",
        ),
    )

    # 4. Move the data into the child (shared id preserves the FK values).
    cols = ", ".join(_CHILD_COLS)
    op.execute(
        f"INSERT INTO auction_lot (id, {cols}) SELECT id, {cols} FROM vehicle_offer"
    )

    # 5. Child indexes (the auction_id / lot_status / scheduled_end_at indexes
    #    that previously lived on auction_lots move here).
    op.create_index("ix_auction_lot_auction_id", "auction_lot", ["auction_id"])
    op.create_index("ix_auction_lot_lot_status", "auction_lot", ["lot_status"])
    op.create_index("ix_auction_lot_scheduled_end_at", "auction_lot", ["scheduled_end_at"])

    # 6. Re-point auction_bid_history at the auction child (it is auction-only).
    op.drop_constraint(
        "fk_auction_bid_history_lot_id_auction_lots", "auction_bid_history",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_auction_bid_history_lot_id_auction_lot", "auction_bid_history",
        "auction_lot", ["lot_id"], ["id"], ondelete="CASCADE",
    )

    # 7. Drop the carved-out columns from the parent. Postgres auto-drops the
    #    indexes / unique key / FK-to-auctions / composite index that depend on
    #    them (incl. ix_auction_lots_price_deal_score via lot_status).
    for col in _CHILD_COLS:
        op.drop_column("vehicle_offer", col)

    # 8. Recreate the decomposed parent deal-score index (price_deal_score only).
    op.create_index(
        "ix_vehicle_offer_price_deal_score", "vehicle_offer", ["price_deal_score"],
    )

    # 9. Rename surviving parent indexes to the vehicle_offer convention.
    for old, new in _PARENT_INDEX_RENAMES:
        op.execute(f"ALTER INDEX {old} RENAME TO {new}")

    # 10. Rename the two parent-pointing inbound FK constraints to convention
    #     (their target auto-followed the table rename to vehicle_offer).
    op.execute(
        "ALTER TABLE purchases RENAME CONSTRAINT "
        "fk_purchases_linked_lot_id_auction_lots TO fk_purchases_linked_lot_id_vehicle_offer"
    )
    op.execute(
        "ALTER TABLE want_matches RENAME CONSTRAINT "
        "fk_want_matches_lot_id_auction_lots TO fk_want_matches_lot_id_vehicle_offer"
    )

    # 11. Create the (empty) private_listing child. Natural key lands in S2.
    op.create_table(
        "private_listing",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("asking_price_cad", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("seller_type", sa.String(length=32), nullable=True),
        sa.Column("days_on_market", sa.Integer(), nullable=True),
        sa.Column("listing_status", sa.String(length=32), server_default="active", nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disappeared_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_private_listing"),
        sa.ForeignKeyConstraint(
            ["id"], ["vehicle_offer.id"],
            name="fk_private_listing_id_vehicle_offer", ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    op.drop_table("private_listing")

    # The pre-split monolith can't represent non-auction offers (no auction_id /
    # source_lot_id / lot_status), and the re-added NOT NULL columns below would
    # fail on private parents (they have no auction_lot child to copy back from).
    # Downgrading inherently discards private listings — delete them honestly.
    op.execute("DELETE FROM vehicle_offer WHERE offer_kind <> 'auction'")

    # Reverse the parent-FK constraint renames.
    op.execute(
        "ALTER TABLE want_matches RENAME CONSTRAINT "
        "fk_want_matches_lot_id_vehicle_offer TO fk_want_matches_lot_id_auction_lots"
    )
    op.execute(
        "ALTER TABLE purchases RENAME CONSTRAINT "
        "fk_purchases_linked_lot_id_vehicle_offer TO fk_purchases_linked_lot_id_auction_lots"
    )

    # Reverse parent index renames + drop the decomposed deal-score index.
    for old, new in _PARENT_INDEX_RENAMES:
        op.execute(f"ALTER INDEX {new} RENAME TO {old}")
    op.drop_index("ix_vehicle_offer_price_deal_score", table_name="vehicle_offer")

    # Re-add the carved-out columns to the parent (nullable for the backfill).
    op.add_column("vehicle_offer", sa.Column("auction_id", sa.BigInteger(), nullable=True))
    op.add_column("vehicle_offer", sa.Column("source_lot_id", sa.String(length=128), nullable=True))
    op.add_column("vehicle_offer", sa.Column("source_lot_row_id", sa.BigInteger(), nullable=True))
    op.add_column("vehicle_offer", sa.Column("lot_number", sa.String(length=64), nullable=True))
    op.add_column("vehicle_offer", sa.Column("scheduled_end_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("vehicle_offer", sa.Column("current_high_bid_cad", sa.Numeric(precision=12, scale=2), nullable=True))
    op.add_column("vehicle_offer", sa.Column("last_bid_observed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("vehicle_offer", sa.Column("bid_count_visible", sa.Integer(), nullable=True))
    op.add_column("vehicle_offer", sa.Column("reserve_met", sa.Boolean(), nullable=True))
    op.add_column("vehicle_offer", sa.Column("lot_status", sa.String(length=32), server_default="open", nullable=True))
    op.add_column("vehicle_offer", sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("vehicle_offer", sa.Column("final_bid_cad", sa.Numeric(precision=12, scale=2), nullable=True))
    op.add_column("vehicle_offer", sa.Column("early_warning_notified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("vehicle_offer", sa.Column("cheap_notified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("vehicle_offer", sa.Column("closing_notified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("vehicle_offer", sa.Column("trajectory_notified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("vehicle_offer", sa.Column("extended_notified_at", sa.DateTime(timezone=True), nullable=True))

    # Copy data back from the child.
    set_clause = ", ".join(f"{c} = al.{c}" for c in _CHILD_COLS)
    op.execute(
        f"UPDATE vehicle_offer vo SET {set_clause} FROM auction_lot al WHERE al.id = vo.id"
    )

    # Restore NOT NULL on the originally-required columns.
    op.alter_column("vehicle_offer", "auction_id", nullable=False)
    op.alter_column("vehicle_offer", "source_lot_id", nullable=False)
    op.alter_column("vehicle_offer", "lot_status", nullable=False)

    # Re-point auction_bid_history back at the parent (still named vehicle_offer
    # here; the FK target follows the rename below).
    op.drop_constraint(
        "fk_auction_bid_history_lot_id_auction_lot", "auction_bid_history",
        type_="foreignkey",
    )
    op.drop_table("auction_lot")
    op.create_foreign_key(
        "fk_auction_bid_history_lot_id_auction_lots", "auction_bid_history",
        "vehicle_offer", ["lot_id"], ["id"], ondelete="CASCADE",
    )

    # Restore the auction-origin constraints + indexes on the parent (named for
    # the auction_lots table they belong to after the rename below).
    op.create_foreign_key(
        "fk_auction_lots_auction_id_auctions", "vehicle_offer", "auctions",
        ["auction_id"], ["id"], ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_auction_lots_auction_source_lot", "vehicle_offer",
        ["auction_id", "source_lot_id"],
    )
    op.create_index("ix_auction_lots_auction_id", "vehicle_offer", ["auction_id"])
    op.create_index("ix_auction_lots_lot_status", "vehicle_offer", ["lot_status"])
    op.create_index("ix_auction_lots_scheduled_end_at", "vehicle_offer", ["scheduled_end_at"])
    op.create_index(
        "ix_auction_lots_price_deal_score", "vehicle_offer",
        ["price_deal_score", "lot_status"],
    )

    # Drop the discriminator and rename the table/sequence/PK back.
    op.drop_column("vehicle_offer", "offer_kind")
    op.execute(
        "ALTER TABLE vehicle_offer RENAME CONSTRAINT pk_vehicle_offer TO pk_auction_lots"
    )
    op.execute("ALTER SEQUENCE vehicle_offer_id_seq RENAME TO auction_lots_id_seq")
    op.rename_table("vehicle_offer", "auction_lots")
