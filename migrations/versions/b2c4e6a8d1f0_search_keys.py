"""LLMSetting 加 ②搜索源 key 列（gemini_api_key / perplexity_api_key）

Revision ID: b2c4e6a8d1f0
Revises: a1b9d7c3e2f5
Create Date: 2026-07-05 07:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = 'b2c4e6a8d1f0'
down_revision: Union[str, None] = 'a1b9d7c3e2f5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('llmsetting', sa.Column(
        'gemini_api_key', sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default=''))
    op.add_column('llmsetting', sa.Column(
        'perplexity_api_key', sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default=''))


def downgrade() -> None:
    op.drop_column('llmsetting', 'perplexity_api_key')
    op.drop_column('llmsetting', 'gemini_api_key')
