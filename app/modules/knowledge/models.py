"""①知识库 数据模型——两级树：品牌 → campaign，各挂文档 + 全局数据池。

这是其他模块的"样板"：怎么定义 SQLModel 表、关系、时间字段。
PoolTopic 是跨模块共享表（见 ARCHITECTURE §6）：①建表、⑤写经验包、②读取，
三方对齐字段、不许各建各的——字段务必与契约一致。
"""
from datetime import date, datetime

from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now()


class Brand(SQLModel, table=True):
    """品牌：知识库顶层，长期不变。"""
    id: int | None = Field(default=None, primary_key=True)
    name: str
    brand_prompt: str = ""        # 品牌定义 prompt
    content_notes: str = ""       # 内容要求
    brand_digest: str = ""        # brand_prompt 的 AI 解析结果（core/llm 生成）
    created_at: datetime = Field(default_factory=_now)


class BrandDoc(SQLModel, table=True):
    """品牌层资料（低时效文档）。"""
    id: int | None = Field(default=None, primary_key=True)
    brand_id: int = Field(foreign_key="brand.id", index=True)
    filename: str
    file_path: str
    created_at: datetime = Field(default_factory=_now)


class Campaign(SQLModel, table=True):
    """活动：品牌下的子级，强时效。is_default=True 为每品牌默认的"品牌日常"（无起止）。"""
    id: int | None = Field(default=None, primary_key=True)
    brand_id: int = Field(foreign_key="brand.id", index=True)
    name: str
    start_date: date | None = None
    end_date: date | None = None
    is_default: bool = False      # 品牌日常常驻 campaign
    campaign_digest: str = ""     # 活动资料的 AI 解析结果（core/llm 生成）
    created_at: datetime = Field(default_factory=_now)


class CampaignDoc(SQLModel, table=True):
    """活动资料（扁平·每份带备注标识，AI 读）。"""
    id: int | None = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaign.id", index=True)
    filename: str
    file_path: str
    note: str = ""                # 备注标识，如"开幕/中期/闭幕"
    created_at: datetime = Field(default_factory=_now)


class PoolTopic(SQLModel, table=True):
    """数据池条目（跨模块共享，ARCHITECTURE §6）。

    ①建表并写"资料包"；⑤沉淀"经验包"(kind='经验包', source='feedback')；②读取喂选题 prompt。
    字段严格对齐契约，②⑤ 直接 import 使用，不要重建。
    """
    id: int | None = Field(default=None, primary_key=True)
    title: str
    kind: str                        # '资料包' | '经验包'
    web_access: bool = True          # 触网 / 不触网
    source: str = "upload"           # 'upload' | 'feedback'(来自⑤) | 'shared'
    brand_tag: str | None = None     # 来源品牌 tag（可空 = 通用）
    content: str = ""                # 解析结果 / 经验摘要
    created_at: datetime = Field(default_factory=_now)
