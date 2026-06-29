"""private_listing previous_asking_price

Revision ID: 2232ad3fac94
Revises: 51338a1ffafb
Create Date: 2026-06-28 15:46:33.857331

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2232ad3fac94'
down_revision: Union[str, Sequence[str], None] = '51338a1ffafb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "private_listing",
        sa.Column("previous_asking_price_cad", sa.Numeric(precision=12, scale=2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("private_listing", "previous_asking_price_cad")
