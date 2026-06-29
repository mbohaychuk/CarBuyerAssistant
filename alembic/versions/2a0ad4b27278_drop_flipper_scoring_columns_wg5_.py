"""drop flipper scoring columns (WG5 teardown)

Revision ID: 2a0ad4b27278
Revises: e97670ee9925
Create Date: 2026-06-28 23:32:30.872302

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2a0ad4b27278'
down_revision: Union[str, Sequence[str], None] = 'e97670ee9925'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop the flipper scoring + trigger-notify columns retired in the WG5
    teardown. The rarity score, flip-resale max bid, the rarity DB inputs, the
    going_cheap flag score, and the early_warning/going_cheap/bid_trajectory
    per-trigger notify stamps no longer have a reader. The closing_soon /
    lot_extended notify columns and all shared valuation columns stay.
    """
    op.drop_index("ix_vehicle_offer_rarity_score", table_name="vehicle_offer")
    op.drop_column("vehicle_offer", "rarity_score")
    op.drop_column("vehicle_offer", "recommended_max_bid_cad")
    op.drop_column("vehicle_offer", "historical_comp_count")
    op.drop_column("vehicle_offer", "recent_appreciation")
    op.drop_column("vehicle_offer", "flag_score")
    op.drop_column("auction_lot", "early_warning_notified_at")
    op.drop_column("auction_lot", "cheap_notified_at")
    op.drop_column("auction_lot", "trajectory_notified_at")


def downgrade() -> None:
    """Re-add the dropped columns (nullable, unpopulated) and the rarity index."""
    op.add_column(
        "auction_lot",
        sa.Column("trajectory_notified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "auction_lot",
        sa.Column("cheap_notified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "auction_lot",
        sa.Column("early_warning_notified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("vehicle_offer", sa.Column("flag_score", sa.Integer(), nullable=True))
    op.add_column("vehicle_offer", sa.Column("recent_appreciation", sa.Float(), nullable=True))
    op.add_column(
        "vehicle_offer", sa.Column("historical_comp_count", sa.Integer(), nullable=True)
    )
    op.add_column(
        "vehicle_offer",
        sa.Column("recommended_max_bid_cad", sa.Numeric(12, 2), nullable=True),
    )
    op.add_column("vehicle_offer", sa.Column("rarity_score", sa.Float(), nullable=True))
    op.create_index("ix_vehicle_offer_rarity_score", "vehicle_offer", ["rarity_score"])
