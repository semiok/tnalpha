"""③写作引擎 数据模型。

Article 按 topic_id 接 ② 的采纳选题；写作/发布状态归③持有，不回写 Topic.status。
Style 归属 campaign，生成文章时可注入默认风格。
"""
from datetime import datetime

from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now()


ARTICLE_STATUSES = ("辩论中", "写作中", "重写中", "待配图", "待选图", "已生成", "已排期", "已发布", "已删除")

# 发布平台选项
PLATFORMS = ("小红书", "微信公众号")

# 风格来源 → UI 标签。stub 仅回退用，不在 UI 显示。
STYLE_SOURCES = {
    "preset": "AI 预设",
    "google": "Google",
    "mp": "公众号",
    "sonar": "深度热点",
    "url": "URL 提取",
    "manual": "手动",
    "stub": "内置",
}


class Style(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaign.id", index=True)
    name: str
    summary: str = ""          # 段落/语气/用词总结
    reference_url: str = ""
    source: str = "stub"       # preset | google | mp | sonar | url | manual | stub
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
    error_message: str = ""
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    generated_at: datetime | None = None
    deleted_at: datetime | None = None
    # 多角色辩论/评审
    debate_rounds: int = 0       # 本次用的辩论轮次（0=跳过辩论）
    review_rounds: int = 0       # 本次用的评审轮次（0=跳过评审）
    debate_brief: str = ""       # 辩论综合出的写作简报
    review_summary: str = ""     # 评审综合摘要
    # 发布平台 & 目标字数
    platform: str = ""           # "小红书" | "微信公众号" | ""
    word_count: int = 0          # 目标字数（0=不限）


class DebateRecord(SQLModel, table=True):
    """多角色辩论/评审记录（持久化每次发言，供查看）。"""
    id: int | None = Field(default=None, primary_key=True)
    article_id: int = Field(foreign_key="article.id", index=True)
    phase: str              # "debate" (辩论) | "review" (评审)
    round_num: int           # 第几轮（从 1 开始）
    role: str                # writer | editor | brand | reader
    content: str             # 该角色本轮发言
    created_at: datetime = Field(default_factory=_now)


class ArticleImage(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    article_id: int = Field(foreign_key="article.id", index=True)
    prompt: str = ""
    image_url: str = ""
    created_at: datetime = Field(default_factory=_now)
    # 多插图候选
    slot_index: int = 0         # 第几个插图位置（0=头图/封面）
    slot_desc: str = ""        # 该位置插图描述（AI 标记的内容）
    is_selected: bool = False  # 用户是否选中此图
