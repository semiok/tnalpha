"""②选题库 数据模型。Topic = §5 契约表（③④⑤ 会读）。

字段 = TopicCandidate（contract.py）落库形状 + campaign_id/status/source。
campaign_id 可空：非空=活动选题，空=品牌常青选题。
"""
from datetime import datetime

from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now()


# 选题状态机（②拥有，③④⑤ 读）：候选 → 采纳(待写作) → 写作中 → 图文完成 → 已排期 → 已发布
TOPIC_STATUSES = ("候选", "采纳", "写作中", "图文完成", "已排期", "已发布")


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
    created_at: datetime = Field(default_factory=_now)
