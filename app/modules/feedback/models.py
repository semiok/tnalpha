"""⑤数据反馈：发布复盘沉淀出的经验包。"""
from datetime import datetime

from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now()


EXPERIENCE_TYPES = ("选题经验", "写作经验")
EXPERIENCE_PLATFORMS = ("通用", "小红书", "微信公众号")
PERFORMANCE_LEVELS = ("高表现", "中表现", "低表现", "数据不足")


class FeedbackExperience(SQLModel, table=True):
    """可被②选题库 / ③写作引擎引用的发布后经验条目。"""
    id: int | None = Field(default=None, primary_key=True)
    brand_id: int = Field(foreign_key="brand.id", index=True)
    campaign_id: int | None = Field(default=None, foreign_key="campaign.id", index=True)
    platform: str = "通用"
    experience_type: str = Field(index=True)
    title: str
    summary: str = ""
    positive_notes: str = ""
    negative_notes: str = ""
    action_advice: str = ""
    performance_level: str = "数据不足"
    source_slot_id: int | None = Field(default=None, foreign_key="scheduleslot.id", index=True)
    article_id: int | None = Field(default=None, foreign_key="article.id", index=True)
    topic_id: int | None = Field(default=None, foreign_key="topic.id", index=True)
    is_active: bool = True
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
