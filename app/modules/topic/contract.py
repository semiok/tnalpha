"""②选题库 ↔ ①知识库 的最小共享契约。

你和 lindong 各写各的选题库，**只需都 import 这个文件**：
  - 读 `KnowledgeContext.load(session, brand_id, campaign_id)` —— ②从①读到的一切（结构化，
    别再读原始 PDF/文档，那是①的活；见 ARCHITECTURE §5.1）。两边读法一致，选题输入就对齐。
  - 产 `TopicCandidate` —— 选题候选的形状（落 Topic 表前的中间结果）。

除此之外各自自由：怎么 prompt、怎么排序、UI 长啥样，都不约束。
"""
from dataclasses import dataclass, field

from sqlmodel import Session, select

from app.modules.knowledge.models import (
    Brand, Campaign, CampaignPoolRef, PoolTopic,
)


@dataclass
class KnowledgeContext:
    """②从①读到的分层输入（见 ARCHITECTURE §5.1）：
    品牌层=约束（怎么写）｜活动层=内容（写什么·时机·素材）｜数据池=调优（素材/经验打法）。"""
    brand_prompt: str = ""        # 主题调性（约束：调性/文风/受众/母题，生成时优先级最高）
    content_notes: str = ""       # 内容要求（规范：字数/平台/史料/配图/尾注）
    doc_digest: str = ""          # 文档解读综合（品牌内容全景）
    style_digest: str = ""        # 综合视觉风格（配图方向）
    campaign_digest: str = ""     # 活动选题简报（6 块；无活动=空 → 品牌常青选题模式）
    pool_materials: list[str] = field(default_factory=list)     # 资料包 content（素材/佐证）
    pool_experiences: list[str] = field(default_factory=list)   # 经验包 content（⑤复盘的打法，供调优先级）

    @property
    def has_campaign(self) -> bool:
        return bool(self.campaign_digest)

    @classmethod
    def load(cls, session: Session, brand_id: int, campaign_id: int | None = None) -> "KnowledgeContext":
        """从库里组装。有 campaign_id=活动选题；无=品牌常青选题（只读品牌层）。
        数据池按引用取（活动引用的条目），资料包/经验包分开。"""
        brand = session.get(Brand, brand_id)
        if brand is None:
            raise ValueError("品牌不存在")
        campaign_digest = ""
        materials: list[str] = []
        experiences: list[str] = []
        if campaign_id:
            camp = session.get(Campaign, campaign_id)
            campaign_digest = camp.campaign_digest if camp else ""
            ref_ids = [r.pool_topic_id for r in session.exec(
                select(CampaignPoolRef).where(CampaignPoolRef.campaign_id == campaign_id)).all()]
            topics = session.exec(select(PoolTopic).where(PoolTopic.id.in_(ref_ids))).all() if ref_ids else []
            for t in topics:
                (experiences if t.kind == "经验包" else materials).append(t.content)
        return cls(
            brand_prompt=brand.brand_prompt, content_notes=brand.content_notes,
            doc_digest=brand.doc_digest, style_digest=brand.style_digest,
            campaign_digest=campaign_digest,
            pool_materials=materials, pool_experiences=experiences)


@dataclass
class TopicCandidate:
    """选题候选（②产出、落 Topic 表前的中间形状）。字段对齐活动简报③选题方向的维度。"""
    title: str                    # 选题标题
    outline: str = ""             # 纲要（100-200字：写什么/切入点/可用素材）
    angle: str = ""               # 切入角度 / 一句话描述
    audience: str = ""            # 受众（城市青年/亲子/艺术爱好者…）
    content_type: str = ""        # 内容类型（种草/深度/攻略/知识…）
    timeliness: str = ""          # 时效强弱（强/中/弱）
    materials: str = ""           # 关联素材（文物尺寸/产品参数/来源…）
    image_hint: str = ""          # 配图方向
    publish_window: str = ""      # 建议发布时机（时效节点）
    source: str = ""              # 来源（campaign③ / brand / 经验包…）
