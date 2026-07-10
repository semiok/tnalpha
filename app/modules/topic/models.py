"""②选题库 数据模型。Topic = §5 契约表（③④⑤ 会读）。

字段 = TopicCandidate（contract.py）落库形状 + campaign_id/status/source。
campaign_id 可空：非空=活动选题，空=品牌常青选题。
"""
from datetime import datetime

from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now()


# 选题状态机（共享词表）：候选 → 采纳(待写作) → 写作中 → 图文完成 → 已排期 → 已发布；删除进入回收站。
#
# 【归属·见 ARCHITECTURE §5.2 ②↔③ 契约】
#   ②选题库只拥有并写入前两态：候选 ↔ 采纳（生成/采纳/取消采纳）。
#   ③写作引擎（lindong）读 status=='采纳' 的选题接手，「写作中/图文完成/已排期/已发布」
#   由③的写作库按 topic_id 持有——②的「已创作/已发布」分类应从③读回，不由②写 Topic.status。
#   （③未接入前，这些下游态无数据，对应 tab 为空；接入方式待③落地后按契约实现。）
TOPIC_STATUSES = ("候选", "采纳", "写作中", "图文完成", "已排期", "已发布", "回收站")


class Topic(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    brand_id: int = Field(foreign_key="brand.id", index=True)
    campaign_id: int | None = Field(default=None, foreign_key="campaign.id", index=True)  # 空=品牌常青
    title: str
    outline: str = ""             # 纲要（写什么/切入点/可用素材）
    angle: str = ""               # 切入角度
    audience: str = ""            # 受众
    content_type: str = ""        # 内容类型（种草/深度/攻略…）
    timeliness: str = ""          # 时效强弱（强/中/弱）
    materials: str = ""           # 关联素材
    image_hint: str = ""          # 配图方向
    publish_window: str = ""      # 建议发布时机
    status: str = "候选"          # 见 TOPIC_STATUSES
    source: str = "generated"     # generated（首次生成）| added（增补去重）
    llm_provider: str = ""        # 生成/补全此选题时使用的文本 provider
    llm_model: str = ""           # 生成/补全此选题时使用的文本模型
    rejection_reason: str = ""    # 进回收站时填写：为什么不采纳，后续沉淀选题经验包
    rejected_at: datetime | None = None
    created_at: datetime = Field(default_factory=_now)
