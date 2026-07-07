"""merge MET-8 writing and topic recycle heads

Revision ID: e2f3a4b5c6d7
Revises: c4d5e6f7a8b9, c7d9a21e4f08
Create Date: 2026-07-07
"""

from typing import Sequence, Union


revision: str = "e2f3a4b5c6d7"
down_revision: Union[str, Sequence[str], None] = (
    "c4d5e6f7a8b9",
    "c7d9a21e4f08",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
