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
    """品牌：知识库顶层，长期不变。品牌定义逻辑照 tngen（AI 解文档→反推调性/要求）。"""
    id: int | None = Field(default=None, primary_key=True)
    name: str
    brand_prompt: str = ""        # 主题调性（人可改，AI 解析后自动填、可覆盖，生成时优先级最高）
    content_notes: str = ""       # 内容要求 / 注意事项（同上）
    doc_digest: str = ""          # 文档解读（综合）——AI 读全部资料文档聚合
    style_digest: str = ""        # 综合视觉风格——AI 深度读图聚合，供配图参考
    analysis_status: str = "idle" # idle | running | done | failed（后台解析状态）
    analysis_error: str = ""
    created_at: datetime = Field(default_factory=_now)


class BrandDoc(SQLModel, table=True):
    """品牌层资料（低时效文档）。ai_analysis=单篇文档解读；深度读图出 style_summary。"""
    id: int | None = Field(default=None, primary_key=True)
    brand_id: int = Field(foreign_key="brand.id", index=True)
    filename: str
    file_path: str
    extracted_text: str = ""      # 上传时抽出的正文（core/docparse），AI 解析读它
    ai_analysis: str = ""         # 单篇文档解读（AI 读 extracted_text）
    style_summary: str = ""       # 单篇视觉风格（深度读图·claude 读 PDF 图片页）
    deep_read: bool = False       # 深度读图开关（需读图的文档勾选，用 vision）
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
    analysis_status: str = "idle" # idle | running | done | failed（后台解析状态，同 brand）
    analysis_error: str = ""
    created_at: datetime = Field(default_factory=_now)


class CampaignDoc(SQLModel, table=True):
    """活动资料（扁平·每份带备注标识，AI 读）。"""
    id: int | None = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaign.id", index=True)
    filename: str
    file_path: str
    note: str = ""                # 备注标识，如"开幕/中期/闭幕"
    extracted_text: str = ""      # 上传时抽出的正文（core/docparse），AI 解析读它
    deep_read: bool = False       # 深度读图开关（同品牌资料：需读图的文档勾选，用 vision 读 PDF）
    created_at: datetime = Field(default_factory=_now)


class CampaignPoolRef(SQLModel, table=True):
    """campaign ⇄ 数据池 引用（多对多）。campaign 只引用数据池条目，不单独上传。"""
    id: int | None = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaign.id", index=True)
    pool_topic_id: int = Field(foreign_key="pooltopic.id", index=True)
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
    content: str = ""                # 解析结果 / 经验摘要（上传文件时=抽出的正文）
    file_path: str = ""              # 上传原文件路径（可空；追加列，②⑤ 只读 content 不受影响）
    deep_read: bool = False          # 深度读图（只对 PDF 有意义；图片自动读图、文字走正文，无需开关）
    created_at: datetime = Field(default_factory=_now)
