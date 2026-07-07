"""MET-8 article status library

Revision ID: 9a7d3c2f1b80
Revises: 4d2e9b7c6a10
Create Date: 2026-07-06 15:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9a7d3c2f1b80"
down_revision: Union[str, None] = "4d2e9b7c6a10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("article", sa.Column("error_message", sa.String(), nullable=False, server_default=""))
    op.add_column("article", sa.Column("generated_at", sa.DateTime(), nullable=True))
    op.add_column("article", sa.Column("deleted_at", sa.DateTime(), nullable=True))
    op.execute("update article set status = '已生成' where status = '图文完成'")


def downgrade() -> None:
    op.execute("update article set status = '图文完成' where status = '已生成'")
    op.drop_column("article", "deleted_at")
    op.drop_column("article", "generated_at")
    op.drop_column("article", "error_message")
