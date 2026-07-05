"""②选题库 Topic 表（§5 契约：brand/campaign_id/status + 候选字段）

Revision ID: a1b9d7c3e2f5
Revises: f8a2c3d1e6b9
Create Date: 2026-07-05 04:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = 'a1b9d7c3e2f5'
down_revision: Union[str, None] = 'f8a2c3d1e6b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'topic',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('brand_id', sa.Integer(), nullable=False),
        sa.Column('campaign_id', sa.Integer(), nullable=True),
        sa.Column('title', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('outline', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('angle', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('audience', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('content_type', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('timeliness', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('materials', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('image_hint', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('publish_window', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('status', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('source', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['brand_id'], ['brand.id']),
        sa.ForeignKeyConstraint(['campaign_id'], ['campaign.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_topic_brand_id', 'topic', ['brand_id'])
    op.create_index('ix_topic_campaign_id', 'topic', ['campaign_id'])


def downgrade() -> None:
    op.drop_index('ix_topic_campaign_id', table_name='topic')
    op.drop_index('ix_topic_brand_id', table_name='topic')
    op.drop_table('topic')
