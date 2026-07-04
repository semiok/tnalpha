"""②↔① 最小共享契约：KnowledgeContext（分层读知识库）/ TopicCandidate。"""
from sqlmodel import Session

from app.modules.knowledge.models import Brand, Campaign, CampaignPoolRef, PoolTopic
from app.modules.topic.contract import KnowledgeContext, TopicCandidate


def _seed(session: Session) -> tuple[int, int]:
    b = Brand(name="敦煌", brand_prompt="调性X", content_notes="规范Y",
              doc_digest="文档Z", style_digest="视觉W")
    session.add(b); session.commit(); session.refresh(b)
    c = Campaign(brand_id=b.id, name="活动", campaign_digest="简报6块")
    session.add(c); session.commit(); session.refresh(c)
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
    assert "素材内容" in kc.pool_materials and "打法内容" in kc.pool_experiences
    assert "打法内容" not in kc.pool_materials                             # 经验包不混进资料包


def test_knowledge_context_load_brand_only(fresh_db):
    with Session(fresh_db) as s:
        bid, _ = _seed(s)
        kc = KnowledgeContext.load(s, bid)                                 # 无 campaign = 品牌常青
    assert kc.brand_prompt == "调性X" and not kc.has_campaign
    assert kc.campaign_digest == "" and kc.pool_materials == []


def test_topic_candidate_shape():
    tc = TopicCandidate(title="一枚汉简", audience="城市青年", timeliness="中")
    assert tc.title == "一枚汉简" and tc.audience == "城市青年" and tc.timeliness == "中"
