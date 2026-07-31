"""Move writing styles from campaign scope to brand scope.

Revision ID: i3d4e5f6a7b8
Revises: h2c3d4e5f6a7
Create Date: 2026-07-31 04:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "i3d4e5f6a7b8"
down_revision: Union[str, None] = "h2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("style")}
    indexes = {index["name"] for index in inspector.get_indexes("style")}

    if "brand_id" not in columns:
        op.add_column("style", sa.Column("brand_id", sa.Integer(), nullable=True))

    if "campaign_id" in columns:
        bind.execute(sa.text(
            """
            UPDATE style
            SET brand_id = (
                SELECT campaign.brand_id
                FROM campaign
                WHERE campaign.id = style.campaign_id
            )
            WHERE brand_id IS NULL
            """
        ))
    bind.execute(sa.text(
        """
        UPDATE style
        SET brand_id = (SELECT MIN(id) FROM brand)
        WHERE brand_id IS NULL
        """
    ))
    missing = bind.execute(sa.text(
        "SELECT COUNT(*) FROM style WHERE brand_id IS NULL"
    )).scalar_one()
    if missing:
        raise RuntimeError("Cannot migrate styles without a brand")

    with op.batch_alter_table("style") as batch_op:
        if "ix_style_campaign_id" in indexes:
            batch_op.drop_index("ix_style_campaign_id")
        if "campaign_id" in columns:
            batch_op.drop_column("campaign_id")
        batch_op.alter_column(
            "brand_id",
            existing_type=sa.Integer(),
            nullable=False,
        )
        batch_op.create_index("ix_style_brand_id", ["brand_id"], unique=False)
        batch_op.create_foreign_key(
            "fk_style_brand_id_brand",
            "brand",
            ["brand_id"],
            ["id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    with op.batch_alter_table("style") as batch_op:
        batch_op.add_column(sa.Column("campaign_id", sa.Integer(), nullable=True))
        batch_op.create_index(
            "ix_style_campaign_id",
            ["campaign_id"],
            unique=False,
        )
        batch_op.create_foreign_key(
            "fk_style_campaign_id_campaign",
            "campaign",
            ["campaign_id"],
            ["id"],
        )

    bind.execute(sa.text(
        """
        UPDATE style
        SET campaign_id = (
            SELECT MIN(campaign.id)
            FROM campaign
            WHERE campaign.brand_id = style.brand_id
        )
        """
    ))

    with op.batch_alter_table("style") as batch_op:
        batch_op.drop_constraint(
            "fk_style_brand_id_brand",
            type_="foreignkey",
        )
        batch_op.drop_index("ix_style_brand_id")
        batch_op.drop_column("brand_id")
