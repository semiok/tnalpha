"""merge schedule weeks to brand-level natural weeks

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-07-08
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d0e1f2a3b4c5"
down_revision: Union[str, None] = "c9d0e1f2a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    weeks = bind.execute(sa.text(
        "select id, brand_id, week_start, week_end "
        "from scheduleweek order by brand_id, week_start, week_end, id"
    )).mappings().all()
    keepers: dict[tuple[int, str, str], int] = {}
    for week in weeks:
        key = (week["brand_id"], str(week["week_start"]), str(week["week_end"]))
        keeper_id = keepers.get(key)
        if keeper_id is None:
            keepers[key] = week["id"]
            continue
        bind.execute(
            sa.text("update scheduleslot set week_id = :keeper_id where week_id = :week_id"),
            {"keeper_id": keeper_id, "week_id": week["id"]},
        )
        bind.execute(
            sa.text("delete from scheduleweek where id = :week_id"),
            {"week_id": week["id"]},
        )
    bind.execute(sa.text("update scheduleweek set campaign_id = null"))


def downgrade() -> None:
    pass
