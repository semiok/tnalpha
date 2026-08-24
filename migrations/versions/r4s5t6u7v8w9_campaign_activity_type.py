"""Add the campaign/column activity type.

Revision ID: r4s5t6u7v8w9
Revises: q3r4s5t6u7v8
Create Date: 2026-08-24 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "r4s5t6u7v8w9"
down_revision: Union[str, None] = "q3r4s5t6u7v8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("campaign")}
    if "activity_type" not in columns:
        with op.batch_alter_table("campaign") as batch_op:
            batch_op.add_column(
                sa.Column("activity_type", sa.String(length=20), nullable=False, server_default="campaign")
            )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("campaign")}
    if "activity_type" in columns:
        with op.batch_alter_table("campaign") as batch_op:
            batch_op.drop_column("activity_type")
