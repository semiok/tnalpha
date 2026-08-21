"""Prevent duplicate writing requirements within a brand.

Revision ID: o1p2q3r4s5t6
Revises: n8c9d0e1f2a3
Create Date: 2026-08-18 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "o1p2q3r4s5t6"
down_revision: Union[str, None] = "n8c9d0e1f2a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "writingreq" not in inspector.get_table_names():
        return

    # 清理历史上已经存在的完全重复记录，保留最早保存的一条，
    # 这样后续才能安全建立唯一索引。
    bind.execute(sa.text(
        """
        DELETE FROM writingreq
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM writingreq
            GROUP BY brand_id, content
        )
        """
    ))

    index_name = "uq_writingreq_brand_content"
    indexes = {idx["name"] for idx in inspector.get_indexes("writingreq")}
    if index_name not in indexes:
        op.create_index(
            index_name,
            "writingreq",
            ["brand_id", "content"],
            unique=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "writingreq" not in inspector.get_table_names():
        return

    index_name = "uq_writingreq_brand_content"
    indexes = {idx["name"] for idx in inspector.get_indexes("writingreq")}
    if index_name in indexes:
        op.drop_index(index_name, table_name="writingreq")
