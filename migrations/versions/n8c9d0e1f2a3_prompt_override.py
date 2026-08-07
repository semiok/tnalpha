"""Add promptoverride table for user-editable prompts.

Revision ID: n8c9d0e1f2a3
Revises: m7b8c9d0e1f2
Create Date: 2026-08-07 15:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "n8c9d0e1f2a3"
down_revision: Union[str, None] = "m7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if "promptoverride" not in tables:
        op.create_table(
            "promptoverride",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("key", sa.String(), nullable=False),
            sa.Column("template", sa.String(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_promptoverride_key", "promptoverride", ["key"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "promptoverride" in inspector.get_table_names():
        indexes = {idx["name"] for idx in inspector.get_indexes("promptoverride")}
        if "ix_promptoverride_key" in indexes:
            op.drop_index("ix_promptoverride_key", table_name="promptoverride")
        op.drop_table("promptoverride")
