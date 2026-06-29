"""vehicle_offer reliability counts

Revision ID: e97670ee9925
Revises: 2232ad3fac94
Create Date: 2026-06-28 16:14:10.231700

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e97670ee9925'
down_revision: Union[str, Sequence[str], None] = '2232ad3fac94'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("vehicle_offer", sa.Column("recall_count", sa.Integer(), nullable=True))
    op.add_column("vehicle_offer", sa.Column("complaint_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("vehicle_offer", "complaint_count")
    op.drop_column("vehicle_offer", "recall_count")
