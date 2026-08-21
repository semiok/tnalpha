"""Track the image slot being replaced by a prompt regeneration.

Revision ID: q3r4s5t6u7v8
Revises: p2q3r4s5t6u7
Create Date: 2026-08-20 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "q3r4s5t6u7v8"
down_revision: Union[str, None] = "p2q3r4s5t6u7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {item["name"] for item in inspector.get_columns("article")}
    if "image_generation_slot" not in columns:
        with op.batch_alter_table("article") as batch_op:
            batch_op.add_column(
                sa.Column("image_generation_slot", sa.Integer(), nullable=False, server_default="-1")
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {item["name"] for item in inspector.get_columns("article")}
    if "image_generation_slot" in columns:
        with op.batch_alter_table("article") as batch_op:
            batch_op.drop_column("image_generation_slot")
