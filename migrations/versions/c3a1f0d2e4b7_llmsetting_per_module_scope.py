"""LLMSetting per-module scope (按模块配置模型，预留接口)

Revision ID: c3a1f0d2e4b7
Revises: b100a49321c2
Create Date: 2026-07-04 04:20:00.000000

单行全局配置 → 多行按 scope 分（default=知识库锚点；其余模块预留）。
已有单行 → scope='default'（server_default 保证）；scope 唯一。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # SQLModel 列类型渲染依赖


# revision identifiers, used by Alembic.
revision: str = 'c3a1f0d2e4b7'
down_revision: Union[str, None] = 'b100a49321c2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default='default' 让已有那行全局配置成为 default 锚点
    with op.batch_alter_table('llmsetting', schema=None) as batch_op:
        batch_op.add_column(sa.Column('scope', sqlmodel.sql.sqltypes.AutoString(),
                                      nullable=False, server_default='default'))
        batch_op.create_index('ix_llmsetting_scope', ['scope'], unique=True)


def downgrade() -> None:
    with op.batch_alter_table('llmsetting', schema=None) as batch_op:
        batch_op.drop_index('ix_llmsetting_scope')
        batch_op.drop_column('scope')
