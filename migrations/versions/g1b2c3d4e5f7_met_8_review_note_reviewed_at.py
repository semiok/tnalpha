"""MET-8 新增审核字段 review_note / reviewed_at / ai_review_summary

Revision ID: g1b2c3d4e5f7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-08 23:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "g1b2c3d4e5f7"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from sqlalchemy import inspect
    bind = op.get_bind()
    insp = inspect(bind)
    art_cols = {c["name"] for c in insp.get_columns("article")}

    # Article 新增审核备注 / 审核时间 / AI 审核意见
    if "review_note" not in art_cols:
        op.add_column("article", sa.Column("review_note", sa.Text(), nullable=False, server_default=""))
    if "reviewed_at" not in art_cols:
        op.add_column("article", sa.Column("reviewed_at", sa.DateTime(), nullable=True))
    if "ai_review_summary" not in art_cols:
        op.add_column("article", sa.Column("ai_review_summary", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("article", "ai_review_summary")
    op.drop_column("article", "reviewed_at")
    op.drop_column("article", "review_note")
