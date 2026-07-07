"""MET-9 schedule tables

Revision ID: f4a5b6c7d8e9
Revises: e2f3a4b5c6d7
Create Date: 2026-07-07
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = "f4a5b6c7d8e9"
down_revision: Union[str, None] = "e2f3a4b5c6d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "scheduleweek",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("brand_id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=True),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("week_end", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["brand_id"], ["brand.id"]),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaign.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scheduleweek_brand_id", "scheduleweek", ["brand_id"])
    op.create_index("ix_scheduleweek_campaign_id", "scheduleweek", ["campaign_id"])

    op.create_table(
        "scheduleslot",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("week_id", sa.Integer(), nullable=False),
        sa.Column("article_id", sa.Integer(), nullable=False),
        sa.Column("topic_id", sa.Integer(), nullable=False),
        sa.Column("brand_id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=True),
        sa.Column("publish_date", sa.Date(), nullable=False),
        sa.Column("publish_time", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("platform", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("published_url", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("notes", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["article_id"], ["article.id"]),
        sa.ForeignKeyConstraint(["brand_id"], ["brand.id"]),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaign.id"]),
        sa.ForeignKeyConstraint(["topic_id"], ["topic.id"]),
        sa.ForeignKeyConstraint(["week_id"], ["scheduleweek.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scheduleslot_article_id", "scheduleslot", ["article_id"])
    op.create_index("ix_scheduleslot_brand_id", "scheduleslot", ["brand_id"])
    op.create_index("ix_scheduleslot_campaign_id", "scheduleslot", ["campaign_id"])
    op.create_index("ix_scheduleslot_publish_date", "scheduleslot", ["publish_date"])
    op.create_index("ix_scheduleslot_topic_id", "scheduleslot", ["topic_id"])
    op.create_index("ix_scheduleslot_week_id", "scheduleslot", ["week_id"])


def downgrade() -> None:
    op.drop_index("ix_scheduleslot_week_id", table_name="scheduleslot")
    op.drop_index("ix_scheduleslot_topic_id", table_name="scheduleslot")
    op.drop_index("ix_scheduleslot_publish_date", table_name="scheduleslot")
    op.drop_index("ix_scheduleslot_campaign_id", table_name="scheduleslot")
    op.drop_index("ix_scheduleslot_brand_id", table_name="scheduleslot")
    op.drop_index("ix_scheduleslot_article_id", table_name="scheduleslot")
    op.drop_table("scheduleslot")
    op.drop_index("ix_scheduleweek_campaign_id", table_name="scheduleweek")
    op.drop_index("ix_scheduleweek_brand_id", table_name="scheduleweek")
    op.drop_table("scheduleweek")
