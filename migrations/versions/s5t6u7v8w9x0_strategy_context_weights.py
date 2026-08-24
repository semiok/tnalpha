"""Add strategy context and activity prompt weights.

Revision ID: s5t6u7v8w9x0
Revises: r4s5t6u7v8w9
Create Date: 2026-08-24 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "s5t6u7v8w9x0"
down_revision: Union[str, None] = "r4s5t6u7v8w9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    campaign_columns = {item["name"] for item in inspector.get_columns("campaign")}
    with op.batch_alter_table("campaign") as batch_op:
        if "brand_weight" not in campaign_columns:
            batch_op.add_column(sa.Column("brand_weight", sa.Integer(), nullable=False, server_default="3"))
        if "strategy_weight" not in campaign_columns:
            batch_op.add_column(sa.Column("strategy_weight", sa.Integer(), nullable=False, server_default="3"))
        if "activity_weight" not in campaign_columns:
            batch_op.add_column(sa.Column("activity_weight", sa.Integer(), nullable=False, server_default="4"))

    tables = set(sa.inspect(bind).get_table_names())
    if "strategy" not in tables:
        op.create_table(
            "strategy",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("brand_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("description", sa.String(), nullable=False),
            sa.Column("strategy_digest", sa.String(), nullable=False),
            sa.Column("analysis_status", sa.String(), nullable=False, server_default="idle"),
            sa.Column("analysis_error", sa.String(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["brand_id"], ["brand.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_strategy_brand_id"), "strategy", ["brand_id"], unique=False)

    if "strategydoc" not in tables:
        op.create_table(
            "strategydoc",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("strategy_id", sa.Integer(), nullable=False),
            sa.Column("filename", sa.String(), nullable=False),
            sa.Column("file_path", sa.String(), nullable=False),
            sa.Column("extracted_text", sa.String(), nullable=False),
            sa.Column("ai_analysis", sa.String(), nullable=False),
            sa.Column("deep_read", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["strategy_id"], ["strategy.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_strategydoc_strategy_id"), "strategydoc", ["strategy_id"], unique=False)

    if "campaignstrategyref" not in tables:
        op.create_table(
            "campaignstrategyref",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("campaign_id", sa.Integer(), nullable=False),
            sa.Column("strategy_id", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["campaign_id"], ["campaign.id"]),
            sa.ForeignKeyConstraint(["strategy_id"], ["strategy.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("campaign_id", "strategy_id", name="uq_campaign_strategy_ref"),
        )
        op.create_index(op.f("ix_campaignstrategyref_campaign_id"), "campaignstrategyref", ["campaign_id"], unique=False)
        op.create_index(op.f("ix_campaignstrategyref_strategy_id"), "campaignstrategyref", ["strategy_id"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "campaignstrategyref" in tables:
        op.drop_index(op.f("ix_campaignstrategyref_strategy_id"), table_name="campaignstrategyref")
        op.drop_index(op.f("ix_campaignstrategyref_campaign_id"), table_name="campaignstrategyref")
        op.drop_table("campaignstrategyref")
    if "strategydoc" in tables:
        op.drop_index(op.f("ix_strategydoc_strategy_id"), table_name="strategydoc")
        op.drop_table("strategydoc")
    if "strategy" in tables:
        op.drop_index(op.f("ix_strategy_brand_id"), table_name="strategy")
        op.drop_table("strategy")

    campaign_columns = {item["name"] for item in sa.inspect(bind).get_columns("campaign")}
    with op.batch_alter_table("campaign") as batch_op:
        for column in ("activity_weight", "strategy_weight", "brand_weight"):
            if column in campaign_columns:
                batch_op.drop_column(column)
