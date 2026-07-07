"""④排期版数据模型。

排期状态由④独立持有；③写作引擎的 Article 只读不写。
"""
from datetime import date, datetime

from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now()


SCHEDULE_STATUSES = ("已排期", "已发布", "已取消")


class ScheduleWeek(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    brand_id: int = Field(foreign_key="brand.id", index=True)
    campaign_id: int | None = Field(default=None, foreign_key="campaign.id", index=True)
    week_start: date
    week_end: date
    created_at: datetime = Field(default_factory=_now)


class ScheduleSlot(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    week_id: int = Field(foreign_key="scheduleweek.id", index=True)
    article_id: int = Field(foreign_key="article.id", index=True)
    topic_id: int = Field(foreign_key="topic.id", index=True)
    brand_id: int = Field(foreign_key="brand.id", index=True)
    campaign_id: int | None = Field(default=None, foreign_key="campaign.id", index=True)
    publish_date: date = Field(index=True)
    publish_time: str = ""
    platform: str = ""
    status: str = "已排期"
    published_url: str = ""
    published_at: datetime | None = None
    notes: str = ""
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
