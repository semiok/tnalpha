"""pooltopic deep_read (数据池条目深度读图开关，PDF 用)

Revision ID: f8a2c3d1e6b9
Revises: e7c4a1b9f2d6
Create Date: 2026-07-04 16:20:00.000000

数据池条目加 deep_read（只对 PDF 有意义；图片自动读图、文字走正文）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f8a2c3d1e6b9'
down_revision: Union[str, None] = 'e7c4a1b9f2d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('pooltopic', schema=None) as batch_op:
        batch_op.add_column(sa.Column('deep_read', sa.Boolean(), nullable=False, server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('pooltopic', schema=None) as batch_op:
        batch_op.drop_column('deep_read')
