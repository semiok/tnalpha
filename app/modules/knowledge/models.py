"""①知识库 数据模型——两级树：品牌 → campaign，各挂文档。

这是其他模块的"样板"：怎么定义 SQLModel 表、关系、时间字段。
（关联信息层 / 全局数据源池 为本模块后续迭代，见模块 spec。）
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
    created_at: datetime = Field(default_factory=_now)


class CampaignDoc(SQLModel, table=True):
    """活动资料（扁平·每份带备注标识，AI 读）。"""
    id: int | None = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaign.id", index=True)
    filename: str
    file_path: str
    note: str = ""                # 备注标识，如"开幕/中期/闭幕"
    created_at: datetime = Field(default_factory=_now)
