"""drop 待选图 status, merge into 待审核

Revision ID: a1c4d5e6f7b8
Revises: f0b3c4d5e6a7
Create Date: 2026-07-08 10:30:00.000000

「待选图」状态合并进「待审核」：AI 默认选中 candidate_idx==0，
用户不换 = 默认认可，所以不再需要单独的待选图中间态。
"""
from typing import Sequence, Union

from alembic import op


revision: str = "a1c4d5e6f7b8"
down_revision: Union[str, None] = "f0b3c4d5e6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE article SET status = '待审核' WHERE status = '待选图'")


def downgrade() -> None:
    # 无法精确还原（待审核 含原本就是待审核的），downgrade 不做拆分
    pass
