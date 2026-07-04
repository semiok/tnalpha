"""LLMSetting codex_model (Codex 授权文本 provider)

Revision ID: d5b2e8f1a9c4
Revises: c3a1f0d2e4b7
Create Date: 2026-07-04 05:40:00.000000

新增 codex 文本 provider（gpt-5.5 思考 high）→ LLMSetting 加 codex_model 列。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # SQLModel 列类型渲染依赖


# revision identifiers, used by Alembic.
revision: str = 'd5b2e8f1a9c4'
down_revision: Union[str, None] = 'c3a1f0d2e4b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('llmsetting', schema=None) as batch_op:
        batch_op.add_column(sa.Column('codex_model', sqlmodel.sql.sqltypes.AutoString(),
                                      nullable=False, server_default='gpt-5.5'))


def downgrade() -> None:
    with op.batch_alter_table('llmsetting', schema=None) as batch_op:
        batch_op.drop_column('codex_model')
