"""merge MET-8 writing and MET-9 schedule migration heads

Revision ID: b6c7d8e9f0a1
Revises: a1c4d5e6f7b8, f4a5b6c7d8e9
Create Date: 2026-07-08
"""
from typing import Sequence, Union


revision: str = "b6c7d8e9f0a1"
down_revision: Union[str, Sequence[str], None] = ("a1c4d5e6f7b8", "f4a5b6c7d8e9")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
