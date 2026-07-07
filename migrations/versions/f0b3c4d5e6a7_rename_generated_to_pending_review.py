"""rename article status 已生成 to 待审核

Revision ID: f0b3c4d5e6a7
Revises: c4d5e6f7a8b9
Create Date: 2026-07-08 10:00:00.000000

文章状态「已生成」改名为「待审核」：文本与配图都完成后进入待审核，
更贴合实际流程（生成完需要人审核，而非"已生成"即终态）。
"""
from typing import Sequence, Union

from alembic import op


revision: str = "f0b3c4d5e6a7"
down_revision: Union[str, None] = "c4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE article SET status = '待审核' WHERE status = '已生成'")


def downgrade() -> None:
    op.execute("UPDATE article SET status = '已生成' WHERE status = '待审核'")
