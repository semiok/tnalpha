"""add schedule metrics

Revision ID: c9d0e1f2a3b4
Revises: b6c7d8e9f0a1
Create Date: 2026-07-08
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = "c9d0e1f2a3b4"
down_revision: Union[str, None] = "b6c7d8e9f0a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "schedulemetric",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slot_id", sa.Integer(), nullable=False),
        sa.Column("article_id", sa.Integer(), nullable=False),
        sa.Column("topic_id", sa.Integer(), nullable=False),
        sa.Column("brand_id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=True),
        sa.Column("wechat_read", sa.Integer(), nullable=False),
        sa.Column("wechat_like", sa.Integer(), nullable=False),
        sa.Column("wechat_share", sa.Integer(), nullable=False),
        sa.Column("xhs_like", sa.Integer(), nullable=False),
        sa.Column("xhs_comment", sa.Integer(), nullable=False),
        sa.Column("xhs_collect", sa.Integer(), nullable=False),
        sa.Column("notes", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["article_id"], ["article.id"]),
        sa.ForeignKeyConstraint(["brand_id"], ["brand.id"]),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaign.id"]),
        sa.ForeignKeyConstraint(["slot_id"], ["scheduleslot.id"]),
        sa.ForeignKeyConstraint(["topic_id"], ["topic.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_schedulemetric_article_id", "schedulemetric", ["article_id"])
    op.create_index("ix_schedulemetric_brand_id", "schedulemetric", ["brand_id"])
    op.create_index("ix_schedulemetric_campaign_id", "schedulemetric", ["campaign_id"])
    op.create_index("ix_schedulemetric_slot_id", "schedulemetric", ["slot_id"], unique=True)
    op.create_index("ix_schedulemetric_topic_id", "schedulemetric", ["topic_id"])


def downgrade() -> None:
    op.drop_index("ix_schedulemetric_topic_id", table_name="schedulemetric")
    op.drop_index("ix_schedulemetric_slot_id", table_name="schedulemetric")
    op.drop_index("ix_schedulemetric_campaign_id", table_name="schedulemetric")
    op.drop_index("ix_schedulemetric_brand_id", table_name="schedulemetric")
    op.drop_index("ix_schedulemetric_article_id", table_name="schedulemetric")
    op.drop_table("schedulemetric")
