"""④排期版数据模型。

排期状态由④独立持有；③写作引擎的 Article 只读不写。
"""
from datetime import date, datetime

from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now()


SCHEDULE_STATUSES = ("已排期", "已发布", "已取消")
DEFAULT_RECOMMEND_PROMPT = """你是内容排期策略师。请按以下规则为可排期文章安排发布周与发布时间：
1. 优先让已有 week 均匀获得推荐，不能把所有内容集中塞进第一周。
2. 如果有多个空 week，先保证每个 week 至少有一篇推荐。
3. 同一个 week 可以混排品牌常青和不同 campaign 的文章，不以 campaign 拆周。
4. 默认发布时间按 09:30、12:30、18:30 循环使用，避免同一时间重复。
5. 已发布、已排期、已取消的内容不要重复推荐。"""


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
    platform: str = ""  # 发布平台；推荐平台来自 Article.platform
    status: str = "已排期"
    published_url: str = ""
    published_at: datetime | None = None
    notes: str = ""
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class ScheduleMetric(SQLModel, table=True):
    """发布后的媒体数据回填，挂在④自己的排期 slot 上。"""
    id: int | None = Field(default=None, primary_key=True)
    slot_id: int = Field(foreign_key="scheduleslot.id", index=True)
    article_id: int = Field(foreign_key="article.id", index=True)
    topic_id: int = Field(foreign_key="topic.id", index=True)
    brand_id: int = Field(foreign_key="brand.id", index=True)
    campaign_id: int | None = Field(default=None, foreign_key="campaign.id", index=True)
    wechat_read: int = 0
    wechat_like: int = 0
    wechat_share: int = 0
    xhs_like: int = 0
    xhs_comment: int = 0
    xhs_collect: int = 0
    notes: str = ""
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class ScheduleSetting(SQLModel, table=True):
    """④排期版运行设置。"""
    id: int | None = Field(default=1, primary_key=True)
    recommend_prompt: str = DEFAULT_RECOMMEND_PROMPT
    updated_at: datetime = Field(default_factory=_now)
