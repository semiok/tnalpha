"""Add styledoc table for manual style upload history.

Revision ID: k5f6a7b8c9d0
Revises: j4e5f6a7b8c9
Create Date: 2026-08-06 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "k5f6a7b8c9d0"
down_revision: Union[str, None] = "j4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if "styledoc" not in tables:
        op.create_table(
            "styledoc",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("brand_id", sa.Integer(), nullable=False),
            sa.Column("style_id", sa.Integer(), nullable=True),
            sa.Column("filename", sa.String(), nullable=False),
            sa.Column("file_path", sa.String(), nullable=False),
            sa.Column("extracted_text", sa.String(), nullable=True),
            sa.Column("note", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["brand_id"], ["brand.id"]),
            sa.ForeignKeyConstraint(["style_id"], ["style.id"]),
        )
        op.create_index("ix_styledoc_brand_id", "styledoc", ["brand_id"])
        op.create_index("ix_styledoc_style_id", "styledoc", ["style_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {idx["name"] for idx in inspector.get_indexes("styledoc")} if "styledoc" in inspector.get_table_names() else set()

    if "ix_styledoc_style_id" in indexes:
        op.drop_index("ix_styledoc_style_id", table_name="styledoc")
    if "ix_styledoc_brand_id" in indexes:
        op.drop_index("ix_styledoc_brand_id", table_name="styledoc")
    if "styledoc" in inspector.get_table_names():
        op.drop_table("styledoc")
