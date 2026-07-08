"""add feedback experience table

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-07-08
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = "f2a3b4c5d6e7"
down_revision: Union[str, None] = "e1f2a3b4c5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if "feedbackexperience" in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        "feedbackexperience",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("brand_id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=True),
        sa.Column("platform", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("experience_type", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("title", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("summary", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("positive_notes", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("negative_notes", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("action_advice", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("performance_level", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("source_slot_id", sa.Integer(), nullable=True),
        sa.Column("article_id", sa.Integer(), nullable=True),
        sa.Column("topic_id", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["article_id"], ["article.id"]),
        sa.ForeignKeyConstraint(["brand_id"], ["brand.id"]),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaign.id"]),
        sa.ForeignKeyConstraint(["source_slot_id"], ["scheduleslot.id"]),
        sa.ForeignKeyConstraint(["topic_id"], ["topic.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_feedbackexperience_article_id", "feedbackexperience", ["article_id"])
    op.create_index("ix_feedbackexperience_brand_id", "feedbackexperience", ["brand_id"])
    op.create_index("ix_feedbackexperience_campaign_id", "feedbackexperience", ["campaign_id"])
    op.create_index("ix_feedbackexperience_experience_type", "feedbackexperience", ["experience_type"])
    op.create_index("ix_feedbackexperience_source_slot_id", "feedbackexperience", ["source_slot_id"])
    op.create_index("ix_feedbackexperience_topic_id", "feedbackexperience", ["topic_id"])


def downgrade() -> None:
    op.drop_index("ix_feedbackexperience_topic_id", table_name="feedbackexperience")
    op.drop_index("ix_feedbackexperience_source_slot_id", table_name="feedbackexperience")
    op.drop_index("ix_feedbackexperience_experience_type", table_name="feedbackexperience")
    op.drop_index("ix_feedbackexperience_campaign_id", table_name="feedbackexperience")
    op.drop_index("ix_feedbackexperience_brand_id", table_name="feedbackexperience")
    op.drop_index("ix_feedbackexperience_article_id", table_name="feedbackexperience")
    op.drop_table("feedbackexperience")
