"""MET-8 writing engine tables

Revision ID: 4d2e9b7c6a10
Revises: b2c4e6a8d1f0
Create Date: 2026-07-06 12:15:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = '4d2e9b7c6a10'
down_revision: Union[str, None] = 'b2c4e6a8d1f0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'style',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('campaign_id', sa.Integer(), nullable=False),
        sa.Column('name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('summary', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('reference_url', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('source', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('is_default', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['campaign_id'], ['campaign.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_style_campaign_id', 'style', ['campaign_id'])

    op.create_table(
        'article',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('topic_id', sa.Integer(), nullable=False),
        sa.Column('campaign_id', sa.Integer(), nullable=True),
        sa.Column('style_id', sa.Integer(), nullable=True),
        sa.Column('title', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('body', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('image_prompt', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('image_url', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('status', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['campaign_id'], ['campaign.id']),
        sa.ForeignKeyConstraint(['style_id'], ['style.id']),
        sa.ForeignKeyConstraint(['topic_id'], ['topic.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_article_campaign_id', 'article', ['campaign_id'])
    op.create_index('ix_article_topic_id', 'article', ['topic_id'])

    op.create_table(
        'articleimage',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('article_id', sa.Integer(), nullable=False),
        sa.Column('prompt', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('image_url', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['article_id'], ['article.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_articleimage_article_id', 'articleimage', ['article_id'])


def downgrade() -> None:
    op.drop_index('ix_articleimage_article_id', table_name='articleimage')
    op.drop_table('articleimage')
    op.drop_index('ix_article_topic_id', table_name='article')
    op.drop_index('ix_article_campaign_id', table_name='article')
    op.drop_table('article')
    op.drop_index('ix_style_campaign_id', table_name='style')
    op.drop_table('style')
