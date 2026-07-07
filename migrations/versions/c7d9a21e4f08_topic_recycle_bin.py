"""Topic recycle bin and rejection reason

Revision ID: c7d9a21e4f08
Revises: b2c4e6a8d1f0
Create Date: 2026-07-06 02:30:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c7d9a21e4f08"
down_revision: Union[str, None] = "b2c4e6a8d1f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("topic", sa.Column("rejection_reason", sa.String(), nullable=False, server_default=""))
    op.add_column("topic", sa.Column("rejected_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("topic", "rejected_at")
    op.drop_column("topic", "rejection_reason")
