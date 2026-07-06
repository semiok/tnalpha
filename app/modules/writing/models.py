"""③写作引擎 数据模型。

Article 按 topic_id 接 ② 的采纳选题；写作/发布状态归③持有，不回写 Topic.status。
Style 归属 campaign，生成文章时可注入默认风格。
"""
from datetime import datetime

from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now()


ARTICLE_STATUSES = ("写作中", "图文完成", "已排期", "已发布")


class Style(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaign.id", index=True)
    name: str
    summary: str = ""          # 段落/语气/用词总结
    reference_url: str = ""
    source: str = "stub"       # stub | google | mp | sonar | manual
    is_default: bool = False
    created_at: datetime = Field(default_factory=_now)


class Article(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    topic_id: int = Field(foreign_key="topic.id", index=True)
    campaign_id: int | None = Field(default=None, foreign_key="campaign.id", index=True)
    style_id: int | None = Field(default=None, foreign_key="style.id")
    title: str
    body: str = ""
    image_prompt: str = ""
    image_url: str = ""
    status: str = "写作中"     # 见 ARTICLE_STATUSES；③自己的状态机
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class ArticleImage(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    article_id: int = Field(foreign_key="article.id", index=True)
    prompt: str = ""
    image_url: str = ""
    created_at: datetime = Field(default_factory=_now)
