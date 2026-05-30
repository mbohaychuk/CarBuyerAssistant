"""private_listings

Revision ID: 351a1762d548
Revises: 01382f1952fe
Create Date: 2026-05-30 09:33:33.796949

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '351a1762d548'
down_revision: Union[str, Sequence[str], None] = '01382f1952fe'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "private_listings",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_listing_id", sa.String(length=128), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("photos", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("pickup_province", sa.String(length=8), nullable=True),
        sa.Column("pickup_city", sa.String(length=128), nullable=True),
        sa.Column("ask_price_cad", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("make", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("trim", sa.String(length=64), nullable=True),
        sa.Column("vin", sa.String(length=32), nullable=True),
        sa.Column("mileage_km", sa.Integer(), nullable=True),
        sa.Column("title_status", sa.String(length=32), server_default="UNKNOWN", nullable=False),
        sa.Column("condition_categorical", sa.String(length=16), nullable=True),
        sa.Column("red_flags", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("green_flags", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("showstopper_flags", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("rarity_score", sa.Double(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("desirable_trim_or_spec", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("classic_or_collector", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("expected_value_cad", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("all_in_cost_cad", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("price_deal_score", sa.Double(), nullable=True),
        sa.Column("flag_score", sa.Integer(), nullable=True),
        sa.Column("confidence_bucket", sa.String(length=16), nullable=True),
        sa.Column("enrichment_status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("valuation_status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("alerted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_alert_price_cad", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("user_action", sa.String(length=16), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_private_listings")),
        sa.UniqueConstraint("source", "source_listing_id", name="uq_private_listings_source_listing"),
    )
    op.create_index("ix_private_listings_make_model_year", "private_listings", ["make", "model", "year"], unique=False)
    op.create_index("ix_private_listings_price_deal_score", "private_listings", ["price_deal_score"], unique=False)
    op.create_index("ix_private_listings_user_action", "private_listings", ["user_action"], unique=False)
    op.create_index(
        "ix_private_listings_pending", "private_listings", ["id"], unique=False,
        postgresql_where=sa.text("enrichment_status = 'pending' OR valuation_status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("ix_private_listings_pending", table_name="private_listings")
    op.drop_index("ix_private_listings_user_action", table_name="private_listings")
    op.drop_index("ix_private_listings_price_deal_score", table_name="private_listings")
    op.drop_index("ix_private_listings_make_model_year", table_name="private_listings")
    op.drop_table("private_listings")
