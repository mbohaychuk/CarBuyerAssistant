"""private_listing location_province

Revision ID: 51338a1ffafb
Revises: d85563d5b35f
Create Date: 2026-06-28 11:58:22.089236

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '51338a1ffafb'
down_revision: Union[str, Sequence[str], None] = 'd85563d5b35f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "private_listing",
        sa.Column("location_province", sa.String(length=8), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("private_listing", "location_province")
