"""Add model provenance fields to generated records.

Revision ID: h2c3d4e5f6a7
Revises: g1b2c3d4e5f7
Create Date: 2026-07-10 01:20:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "h2c3d4e5f6a7"
down_revision: Union[str, None] = "g1b2c3d4e5f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _add_text_column(table: str, name: str, existing: set[str]) -> None:
    if name not in existing:
        op.add_column(table, sa.Column(name, sa.Text(), nullable=False, server_default=""))


def upgrade() -> None:
    from sqlalchemy import inspect

    bind = op.get_bind()
    insp = inspect(bind)
    topic_cols = {c["name"] for c in insp.get_columns("topic")}
    article_cols = {c["name"] for c in insp.get_columns("article")}
    image_cols = {c["name"] for c in insp.get_columns("articleimage")}
    exp_cols = {c["name"] for c in insp.get_columns("feedbackexperience")}

    _add_text_column("topic", "llm_provider", topic_cols)
    _add_text_column("topic", "llm_model", topic_cols)

    _add_text_column("article", "llm_provider", article_cols)
    _add_text_column("article", "llm_model", article_cols)

    _add_text_column("articleimage", "image_provider", image_cols)
    _add_text_column("articleimage", "image_model", image_cols)

    _add_text_column("feedbackexperience", "llm_provider", exp_cols)
    _add_text_column("feedbackexperience", "llm_model", exp_cols)


def downgrade() -> None:
    for table, columns in (
        ("feedbackexperience", ("llm_model", "llm_provider")),
        ("articleimage", ("image_model", "image_provider")),
        ("article", ("llm_model", "llm_provider")),
        ("topic", ("llm_model", "llm_provider")),
    ):
        for column in columns:
            op.drop_column(table, column)
