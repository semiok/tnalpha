"""Add writingreq table and article.writing_req column.

Revision ID: l6a7b8c9d0e1
Revises: k5f6a7b8c9d0
Create Date: 2026-08-07 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "l6a7b8c9d0e1"
down_revision: Union[str, None] = "k5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    # 1. 创建 writingreq 表
    if "writingreq" not in tables:
        op.create_table(
            "writingreq",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("brand_id", sa.Integer(), nullable=False),
            sa.Column("title", sa.String(), nullable=False),
            sa.Column("content", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["brand_id"], ["brand.id"]),
        )
        op.create_index("ix_writingreq_brand_id", "writingreq", ["brand_id"])

    # 2. 给 article 表加 writing_req 列
    if "article" in tables:
        columns = {col["name"] for col in inspector.get_columns("article")}
        if "writing_req" not in columns:
            op.add_column(
                "article",
                sa.Column(
                    "writing_req", sa.String(), nullable=False, server_default=""
                ),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 移除 article.writing_req 列
    if "article" in inspector.get_table_names():
        columns = {col["name"] for col in inspector.get_columns("article")}
        if "writing_req" in columns:
            op.drop_column("article", "writing_req")

    # 删除 writingreq 表
    if "writingreq" in inspector.get_table_names():
        indexes = {idx["name"] for idx in inspector.get_indexes("writingreq")}
        if "ix_writingreq_brand_id" in indexes:
            op.drop_index("ix_writingreq_brand_id", table_name="writingreq")
        op.drop_table("writingreq")
