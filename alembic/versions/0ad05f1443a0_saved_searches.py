"""saved_searches

Revision ID: 0ad05f1443a0
Revises: 6b193e16678e
Create Date: 2026-05-28 14:23:37.621098

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '0ad05f1443a0'
down_revision: Union[str, Sequence[str], None] = '6b193e16678e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "saved_searches",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("make", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("trim", sa.String(length=64), nullable=True),
        sa.Column("year_min", sa.Integer(), nullable=True),
        sa.Column("year_max", sa.Integer(), nullable=True),
        sa.Column("mileage_km_max", sa.Integer(), nullable=True),
        sa.Column("title_status", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("condition_categorical", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("province", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("max_all_in_cost_cad", sa.Integer(), nullable=True),
        sa.Column("last_viewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_saved_searches")),
    )
    op.create_table(
        "saved_search_matches",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("saved_search_id", sa.BigInteger(), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("matched_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["saved_search_id"], ["saved_searches.id"],
            name=op.f("fk_saved_search_matches_saved_search_id_saved_searches"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_saved_search_matches")),
        sa.UniqueConstraint(
            "saved_search_id", "source_kind", "source_id",
            name="uq_saved_search_matches_search_source",
        ),
    )
    op.create_index(
        "ix_saved_search_matches_source", "saved_search_matches",
        ["source_kind", "source_id"], unique=False,
    )
    op.create_index(
        "ix_saved_search_matches_active", "saved_search_matches",
        ["saved_search_id", "matched_at"], unique=False,
        postgresql_where=sa.text("dismissed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_saved_search_matches_active", table_name="saved_search_matches")
    op.drop_index("ix_saved_search_matches_source", table_name="saved_search_matches")
    op.drop_table("saved_search_matches")
    op.drop_table("saved_searches")
