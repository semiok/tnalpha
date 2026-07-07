"""MET-8 多角色辩论/评审记录表 + Article 新字段

Revision ID: f1a2b3c4d5e6
Revises: 9a7d3c2f1b80
Create Date: 2026-07-06 23:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, None] = "9a7d3c2f1b80"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Article 新增辩论/评审字段
    op.add_column("article", sa.Column("debate_rounds", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("article", sa.Column("review_rounds", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("article", sa.Column("debate_brief", sa.Text(), nullable=False, server_default=""))
    op.add_column("article", sa.Column("review_summary", sa.Text(), nullable=False, server_default=""))

    # 辩论/评审记录表
    op.create_table(
        "debatercord",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("article_id", sa.Integer(), sa.ForeignKey("article.id"), index=True, nullable=False),
        sa.Column("phase", sa.String(), nullable=False),
        sa.Column("round_num", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("debatercord")
    op.drop_column("article", "review_summary")
    op.drop_column("article", "debate_brief")
    op.drop_column("article", "review_rounds")
    op.drop_column("article", "debate_rounds")
