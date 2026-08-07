"""Drop writingreq.title column (no longer needed, use content truncation for display).

Revision ID: m7b8c9d0e1f2
Revises: l6a7b8c9d0e1
Create Date: 2026-08-07 14:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "m7b8c9d0e1f2"
down_revision: Union[str, None] = "l6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "writingreq" in inspector.get_table_names():
        columns = {col["name"] for col in inspector.get_columns("writingreq")}
        if "title" in columns:
            op.drop_column("writingreq", "title")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "writingreq" in inspector.get_table_names():
        columns = {col["name"] for col in inspector.get_columns("writingreq")}
        if "title" not in columns:
            op.add_column("writingreq", sa.Column("title", sa.String(), nullable=False, server_default=""))
