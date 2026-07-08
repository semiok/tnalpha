"""add source campaign to pool topic

Revision ID: a1b2c3d4e5f6
Revises: f2a3b4c5d6e7
Create Date: 2026-07-08
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "f2a3b4c5d6e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {col["name"] for col in sa.inspect(bind).get_columns("pooltopic")}
    if "source_campaign_id" not in columns:
        op.add_column("pooltopic", sa.Column("source_campaign_id", sa.Integer(), nullable=True))
    indexes = {idx["name"] for idx in sa.inspect(bind).get_indexes("pooltopic")}
    if "ix_pooltopic_source_campaign_id" not in indexes:
        op.create_index("ix_pooltopic_source_campaign_id", "pooltopic", ["source_campaign_id"])


def downgrade() -> None:
    bind = op.get_bind()
    indexes = {idx["name"] for idx in sa.inspect(bind).get_indexes("pooltopic")}
    if "ix_pooltopic_source_campaign_id" in indexes:
        op.drop_index("ix_pooltopic_source_campaign_id", table_name="pooltopic")
    columns = {col["name"] for col in sa.inspect(bind).get_columns("pooltopic")}
    if "source_campaign_id" in columns:
        op.drop_column("pooltopic", "source_campaign_id")
