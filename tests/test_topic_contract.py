"""②↔① 最小共享契约：KnowledgeContext（分层读知识库）/ TopicCandidate。"""
from sqlmodel import Session

from app.modules.knowledge.models import (
    Brand, Campaign, CampaignPoolRef, CampaignStrategyRef, PoolTopic, Strategy,
)
from app.modules.topic.contract import KnowledgeContext, TopicCandidate


def _seed(session: Session) -> tuple[int, int]:
    b = Brand(name="敦煌", brand_prompt="调性X", content_notes="规范Y",
              doc_digest="文档Z", style_digest="视觉W")
    session.add(b); session.commit(); session.refresh(b)
    c = Campaign(
        brand_id=b.id, name="活动", campaign_digest="简报6块",
        brand_weight=2, strategy_weight=5, activity_weight=3,
    )
    session.add(c); session.commit(); session.refresh(c)
    strategy = Strategy(
        brand_id=b.id, name="年轻化策略", strategy_digest="优先面向城市青年",
    )
    session.add(strategy); session.commit(); session.refresh(strategy)
    session.add(CampaignStrategyRef(campaign_id=c.id, strategy_id=strategy.id))
    mat = PoolTopic(title="资料", kind="资料包", content="素材内容")
    exp = PoolTopic(title="复盘", kind="经验包", content="打法内容")
    session.add(mat); session.add(exp); session.commit()
    session.refresh(mat); session.refresh(exp)
    session.add(CampaignPoolRef(campaign_id=c.id, pool_topic_id=mat.id))
    session.add(CampaignPoolRef(campaign_id=c.id, pool_topic_id=exp.id))
    session.commit()
    return b.id, c.id


def test_knowledge_context_load_campaign(fresh_db):
    with Session(fresh_db) as s:
        bid, cid = _seed(s)
        kc = KnowledgeContext.load(s, bid, cid)
    assert kc.brand_prompt == "调性X" and kc.content_notes == "规范Y"       # 品牌层（约束）
    assert kc.doc_digest == "文档Z" and kc.style_digest == "视觉W"
    assert kc.campaign_digest == "简报6块" and kc.has_campaign             # 活动层（内容）
    assert kc.campaign_name == "活动" and kc.activity_type == "campaign"
    assert any("年轻化策略" in item and "城市青年" in item for item in kc.strategy_contexts)
    assert (kc.brand_weight, kc.strategy_weight, kc.activity_weight) == (2, 5, 3)
    assert "素材内容" in kc.pool_materials and "打法内容" in kc.pool_experiences
    assert "打法内容" not in kc.pool_materials                             # 经验包不混进资料包


def test_knowledge_context_load_brand_only(fresh_db):
    with Session(fresh_db) as s:
        bid, _ = _seed(s)
        kc = KnowledgeContext.load(s, bid)                                 # 无 campaign = 品牌常青
    assert kc.brand_prompt == "调性X" and not kc.has_campaign
    assert kc.campaign_digest == "" and kc.pool_materials == []
    assert kc.campaign_name == "" and kc.activity_type == ""


def test_knowledge_context_keeps_unparsed_campaign_scope(fresh_db):
    with Session(fresh_db) as s:
        brand = Brand(name="溯肤")
        s.add(brand); s.commit(); s.refresh(brand)
        campaign = Campaign(brand_id=brand.id, name="球袜", activity_type="column")
        s.add(campaign); s.commit(); s.refresh(campaign)
        kc = KnowledgeContext.load(s, brand.id, campaign.id)
    assert kc.has_campaign
    assert kc.campaign_name == "球袜" and kc.activity_type == "column"
    assert kc.campaign_digest == ""


def test_topic_candidate_shape():
    tc = TopicCandidate(title="一枚汉简", audience="城市青年", timeliness="中")
    assert tc.title == "一枚汉简" and tc.audience == "城市青年" and tc.timeliness == "中"
