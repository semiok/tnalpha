"""campaign analysis_status/deep_read + pooltopic file_path

Revision ID: e7c4a1b9f2d6
Revises: d5b2e8f1a9c4
Create Date: 2026-07-04 15:10:00.000000

- campaign 加 analysis_status/analysis_error（活动资料 AI 解析异步化，同 brand）
- campaigndoc 加 deep_read（活动资料支持深度读图）
- pooltopic 加 file_path（数据池支持资料上传；追加列，②⑤ 只读 content 不受影响）
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # SQLModel 列类型渲染依赖


# revision identifiers, used by Alembic.
revision: str = 'e7c4a1b9f2d6'
down_revision: Union[str, None] = 'd5b2e8f1a9c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('campaign', schema=None) as batch_op:
        batch_op.add_column(sa.Column('analysis_status', sqlmodel.sql.sqltypes.AutoString(),
                                      nullable=False, server_default='idle'))
        batch_op.add_column(sa.Column('analysis_error', sqlmodel.sql.sqltypes.AutoString(),
                                      nullable=False, server_default=''))
    with op.batch_alter_table('campaigndoc', schema=None) as batch_op:
        batch_op.add_column(sa.Column('deep_read', sa.Boolean(), nullable=False, server_default='0'))
    with op.batch_alter_table('pooltopic', schema=None) as batch_op:
        batch_op.add_column(sa.Column('file_path', sqlmodel.sql.sqltypes.AutoString(),
                                      nullable=False, server_default=''))


def downgrade() -> None:
    with op.batch_alter_table('pooltopic', schema=None) as batch_op:
        batch_op.drop_column('file_path')
    with op.batch_alter_table('campaigndoc', schema=None) as batch_op:
        batch_op.drop_column('deep_read')
    with op.batch_alter_table('campaign', schema=None) as batch_op:
        batch_op.drop_column('analysis_error')
        batch_op.drop_column('analysis_status')
