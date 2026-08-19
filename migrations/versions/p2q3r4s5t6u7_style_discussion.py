"""Add persistent AI discussions and revisions for writing styles.

Revision ID: p2q3r4s5t6u7
Revises: o1p2q3r4s5t6
Create Date: 2026-08-18 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "p2q3r4s5t6u7"
down_revision: Union[str, None] = "o1p2q3r4s5t6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "stylediscussion" not in tables:
        op.create_table(
            "stylediscussion",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("style_id", sa.Integer(), nullable=False),
            sa.Column("brand_id", sa.Integer(), nullable=False),
            sa.Column("draft_name", sa.String(), nullable=False, server_default=""),
            sa.Column("draft_summary", sa.String(), nullable=False, server_default=""),
            sa.Column("draft_reason", sa.String(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.Column("last_applied_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["style_id"], ["style.id"]),
            sa.ForeignKeyConstraint(["brand_id"], ["brand.id"]),
        )
        op.create_index("ix_stylediscussion_style_id", "stylediscussion", ["style_id"])
        op.create_index("ix_stylediscussion_brand_id", "stylediscussion", ["brand_id"])
        op.create_index("uq_stylediscussion_style", "stylediscussion", ["style_id"], unique=True)

    if "stylediscussionmessage" not in tables:
        op.create_table(
            "stylediscussionmessage",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("discussion_id", sa.Integer(), nullable=False),
            sa.Column("role", sa.String(), nullable=False),
            sa.Column("content", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["discussion_id"], ["stylediscussion.id"]),
        )
        op.create_index(
            "ix_stylediscussionmessage_discussion_id",
            "stylediscussionmessage",
            ["discussion_id"],
        )

    if "stylerevision" not in tables:
        op.create_table(
            "stylerevision",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("style_id", sa.Integer(), nullable=False),
            sa.Column("discussion_id", sa.Integer(), nullable=True),
            sa.Column("previous_name", sa.String(), nullable=False),
            sa.Column("previous_summary", sa.String(), nullable=False),
            sa.Column("new_name", sa.String(), nullable=False),
            sa.Column("new_summary", sa.String(), nullable=False),
            sa.Column("change_reason", sa.String(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["style_id"], ["style.id"]),
            sa.ForeignKeyConstraint(["discussion_id"], ["stylediscussion.id"]),
        )
        op.create_index("ix_stylerevision_style_id", "stylerevision", ["style_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "stylerevision" in tables:
        op.drop_index("ix_stylerevision_style_id", table_name="stylerevision")
        op.drop_table("stylerevision")
    if "stylediscussionmessage" in tables:
        op.drop_index(
            "ix_stylediscussionmessage_discussion_id",
            table_name="stylediscussionmessage",
        )
        op.drop_table("stylediscussionmessage")
    if "stylediscussion" in tables:
        op.drop_index("uq_stylediscussion_style", table_name="stylediscussion")
        op.drop_index("ix_stylediscussion_brand_id", table_name="stylediscussion")
        op.drop_index("ix_stylediscussion_style_id", table_name="stylediscussion")
        op.drop_table("stylediscussion")
