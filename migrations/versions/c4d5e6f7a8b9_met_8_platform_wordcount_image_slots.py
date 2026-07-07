"""MET-8 新增发布平台/目标字数/多插图候选

Revision ID: c4d5e6f7a8b9
Revises: f1a2b3c4d5e6
Create Date: 2026-07-07 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from sqlalchemy import inspect
    bind = op.get_bind()
    insp = inspect(bind)
    art_cols = {c["name"] for c in insp.get_columns("article")}
    img_cols = {c["name"] for c in insp.get_columns("articleimage")}

    # Article 新增发布平台/目标字数
    if "platform" not in art_cols:
        op.add_column("article", sa.Column("platform", sa.String(), nullable=False, server_default=""))
    if "word_count" not in art_cols:
        op.add_column("article", sa.Column("word_count", sa.Integer(), nullable=False, server_default="0"))

    # ArticleImage 新增插图位置/描述/选中标记
    if "slot_index" not in img_cols:
        op.add_column("articleimage", sa.Column("slot_index", sa.Integer(), nullable=False, server_default="0"))
    if "slot_desc" not in img_cols:
        op.add_column("articleimage", sa.Column("slot_desc", sa.Text(), nullable=False, server_default=""))
    if "is_selected" not in img_cols:
        op.add_column("articleimage", sa.Column("is_selected", sa.Boolean(), nullable=False, server_default=sa.text("0")))


def downgrade() -> None:
    op.drop_column("articleimage", "is_selected")
    op.drop_column("articleimage", "slot_desc")
    op.drop_column("articleimage", "slot_index")
    op.drop_column("article", "word_count")
    op.drop_column("article", "platform")
