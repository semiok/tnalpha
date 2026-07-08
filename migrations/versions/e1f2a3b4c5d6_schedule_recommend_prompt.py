"""add schedule recommend prompt setting

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-07-08
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, None] = "d0e1f2a3b4c5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_RECOMMEND_PROMPT = """你是内容排期策略师。请按以下规则为可排期文章安排发布周与发布时间：
1. 优先让已有 week 均匀获得推荐，不能把所有内容集中塞进第一周。
2. 如果有多个空 week，先保证每个 week 至少有一篇推荐。
3. 同一个 week 可以混排品牌常青和不同 campaign 的文章，不以 campaign 拆周。
4. 默认发布时间按 09:30、12:30、18:30 循环使用，避免同一时间重复。
5. 已发布、已排期、已取消的内容不要重复推荐。"""


def upgrade() -> None:
    bind = op.get_bind()
    if "schedulesetting" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "schedulesetting",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("recommend_prompt", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
    exists = bind.execute(sa.text("select count(*) from schedulesetting where id = 1")).scalar_one()
    if not exists:
        bind.execute(
            sa.text(
                "insert into schedulesetting (id, recommend_prompt, updated_at) "
                "values (1, :prompt, CURRENT_TIMESTAMP)"
            ),
            {"prompt": DEFAULT_RECOMMEND_PROMPT},
        )


def downgrade() -> None:
    op.drop_table("schedulesetting")
