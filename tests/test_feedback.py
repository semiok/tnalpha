"""⑤数据反馈：发布样本 → 经验包 → ②/③引用。"""
from datetime import datetime

from sqlmodel import Session, select

from app.modules.feedback.experience import experience_pack_text
from app.modules.feedback.models import FeedbackExperience
from app.modules.knowledge.models import Brand, Campaign, CampaignPoolRef, PoolTopic
from app.modules.schedule import schedule
from app.modules.schedule.models import ScheduleMetric
from app.modules.topic.generate import generate_topics
from app.modules.topic.models import Topic
from app.modules.writing.models import Article
from app.modules.writing.routes import _article_prompt


def _seed_published(session: Session) -> dict[str, int]:
    brand = Brand(name="敦煌IP")
    session.add(brand)
    session.commit()
    session.refresh(brand)
    campaign = Campaign(brand_id=brand.id, name="丝路有多长")
    session.add(campaign)
    session.commit()
    session.refresh(campaign)
    topic = Topic(brand_id=brand.id, campaign_id=campaign.id, title="习字简选题", outline="从习字简切入日常书写", status="采纳")
    session.add(topic)
    session.commit()
    session.refresh(topic)
    article = Article(
        topic_id=topic.id,
        campaign_id=campaign.id,
        title="在边塞练字的人",
        body="标题：在边塞练字的人\n\n正文：从一枚习字简看丝路日常。",
        status="已审核",
        generated_at=datetime(2026, 7, 7, 9, 0),
        platform="小红书",
    )
    session.add(article)
    session.commit()
    session.refresh(article)
    week = schedule.add_week(session, brand.id)
    slot = schedule.add_slot(session, week.id, article.id, week.week_start, "09:30")
    slot = schedule.publish_slot(session, slot.id, "小红书", "https://example.com/xhs", datetime(2026, 7, 8, 10, 0))
    metric = ScheduleMetric(
        slot_id=slot.id,
        article_id=article.id,
        topic_id=topic.id,
        brand_id=brand.id,
        campaign_id=campaign.id,
        xhs_like=80,
        xhs_comment=12,
        xhs_collect=40,
        notes="评论区对日常细节很感兴趣",
    )
    session.add(metric)
    session.commit()
    return {
        "brand_id": brand.id,
        "campaign_id": campaign.id,
        "topic_id": topic.id,
        "article_id": article.id,
        "slot_id": slot.id,
    }


def test_feedback_page_creates_experience_from_published_sample(owner_client, fresh_db, monkeypatch):
    monkeypatch.setattr(
        "app.modules.feedback.experience.llm.text_model_info",
        lambda module="default": ("minimax-m3", "MiniMax-M3"),
    )
    with Session(fresh_db) as session:
        ids = _seed_published(session)

    page = owner_client.get("/feedback")
    assert page.status_code == 200
    assert "发布文章复盘" in page.text
    assert "在边塞练字的人" in page.text
    assert "待总结" in page.text
    assert "生成经验" in page.text

    created = owner_client.post(
        "/feedback/experiences/from-slot",
        data={"slot_id": ids["slot_id"], "scope": "campaign", "platform": "小红书"},
        follow_redirects=False,
    )
    assert created.status_code == 303
    assert created.headers["location"].startswith("/feedback")
    assert f"open={ids['slot_id']}" in created.headers["location"]

    with Session(fresh_db) as session:
        entries = session.exec(
            select(FeedbackExperience).where(FeedbackExperience.source_slot_id == ids["slot_id"])
        ).all()
        assert len(entries) == 2
        assert {entry.experience_type for entry in entries} == {"选题经验", "写作经验"}
        assert all(entry.brand_id == ids["brand_id"] for entry in entries)
        assert all(entry.campaign_id == ids["campaign_id"] for entry in entries)
        assert all(entry.platform == "小红书" for entry in entries)
        assert all(entry.performance_level == "高表现" for entry in entries)
        assert all(entry.llm_provider == "minimax-m3" for entry in entries)
        assert all(entry.llm_model == "MiniMax-M3" for entry in entries)

    updated = owner_client.post(
        f"/feedback/experiences/slot/{ids['slot_id']}/update",
        data={
            "topic_title": "选题判断",
            "topic_summary": "选题综合分析",
            "topic_positive_notes": "物件切口有效",
            "topic_negative_notes": "不要空泛",
            "topic_action_advice": "给选题库复用",
            "writing_title": "写作判断",
            "writing_summary": "写作综合分析",
            "writing_positive_notes": "开头轻",
            "writing_negative_notes": "不要堆资料",
            "writing_action_advice": "给写作引擎复用",
        },
        follow_redirects=False,
    )
    assert updated.status_code == 303
    with Session(fresh_db) as session:
        entries = session.exec(
            select(FeedbackExperience).where(FeedbackExperience.source_slot_id == ids["slot_id"])
        ).all()
        by_type = {entry.experience_type: entry for entry in entries}
        assert by_type["选题经验"].title == "选题判断"
        assert by_type["选题经验"].action_advice == "给选题库复用"
        assert by_type["写作经验"].title == "写作判断"
        assert by_type["写作经验"].action_advice == "给写作引擎复用"

    summarized_page = owner_client.get("/feedback?status=已总结")
    assert summarized_page.status_code == 200
    assert "在边塞练字的人" in summarized_page.text
    assert "综合分析" in summarized_page.text
    assert "给②选题库的复用指令" in summarized_page.text
    assert "给③写作引擎的复用指令" in summarized_page.text
    pending_page = owner_client.get("/feedback?status=待总结")
    assert pending_page.status_code == 200
    assert "在边塞练字的人" not in pending_page.text


def test_feedback_batch_generates_article_experience_pairs(owner_client, fresh_db, monkeypatch):
    def fake_draft(session, slot_id, experience_type):
        return {
            "title": f"{experience_type} #{slot_id}",
            "summary": "批量总结",
            "positive_notes": "保留有效切口",
            "negative_notes": "避免空泛",
            "action_advice": "下次按文章经验复用",
        }

    monkeypatch.setattr("app.modules.feedback.experience.build_experience_draft", fake_draft)
    with Session(fresh_db) as session:
        ids = _seed_published(session)
        topic = Topic(
            brand_id=ids["brand_id"],
            campaign_id=ids["campaign_id"],
            title="驿站石头选题",
            outline="从遗址石头切入交通",
            status="采纳",
        )
        session.add(topic)
        session.commit()
        session.refresh(topic)
        article = Article(
            topic_id=topic.id,
            campaign_id=ids["campaign_id"],
            title="一个汉代驿站消失了两千年",
            body="正文：驿站和丝路交通。",
            status="已审核",
            generated_at=datetime(2026, 7, 7, 10, 0),
            platform="微信公众号",
        )
        session.add(article)
        session.commit()
        session.refresh(article)
        week = schedule.add_week(session, ids["brand_id"])
        slot = schedule.add_slot(session, week.id, article.id, week.week_start, "10:30")
        slot = schedule.publish_slot(session, slot.id, "微信公众号", "https://example.com/wx", datetime(2026, 7, 8, 11, 0))
        session.add(ScheduleMetric(
            slot_id=slot.id,
            article_id=article.id,
            topic_id=topic.id,
            brand_id=ids["brand_id"],
            campaign_id=ids["campaign_id"],
            wechat_read=500,
            wechat_like=20,
            wechat_share=8,
        ))
        session.commit()
        slot_ids = [ids["slot_id"], slot.id]

    created = owner_client.post(
        "/feedback/experiences/batch",
        data={"slot_id": [slot_ids[0], slot_ids[1]], "scope": "campaign"},
        follow_redirects=False,
    )
    assert created.status_code == 303

    with Session(fresh_db) as session:
        entries = session.exec(select(FeedbackExperience)).all()
        assert len(entries) == 4
        assert {entry.source_slot_id for entry in entries} == set(slot_ids)
        assert {entry.experience_type for entry in entries} == {"选题经验", "写作经验"}


def test_topic_generation_can_reference_publish_experience(monkeypatch, fresh_db):
    seen = {}

    def fake_generate(prompt, **kwargs):
        seen["prompt"] = prompt
        return "标题：新的习字简选题\n纲要：从具体物件切入。\n受众：亲子\n时效：中\n素材：习字简\n配图：简牍\n时机：近期"

    monkeypatch.setattr("app.modules.topic.generate.llm.generate_text", fake_generate)
    with Session(fresh_db) as session:
        ids = _seed_published(session)
        pack = PoolTopic(
            title="经验包｜丝路有多长",
            kind="经验包",
            source="feedback",
            content="具体物件切口有效\n标题里保留具体物件和问题",
        )
        session.add(pack)
        session.commit()
        session.refresh(pack)
        session.add(CampaignPoolRef(campaign_id=ids["campaign_id"], pool_topic_id=pack.id))
        session.commit()
        generate_topics(session, ids["brand_id"], ids["campaign_id"], count=1)

    assert "Campaign 总体经验包" in seen["prompt"]
    assert "具体物件切口有效" in seen["prompt"]
    assert "标题里保留具体物件和问题" in seen["prompt"]


def test_writing_prompt_can_reference_publish_experience(fresh_db):
    from app.modules.topic.contract import KnowledgeContext

    topic = Topic(brand_id=1, campaign_id=None, title="习字简", outline="从练字切入", status="采纳")
    ctx = KnowledgeContext(
        brand_prompt="品牌调性",
        content_notes="内容要求",
        doc_digest="资料综合",
        campaign_digest="",
        pool_materials=[],
        pool_experiences=[],
    )
    prompt = _article_prompt(
        topic,
        ctx,
        None,
        "小红书",
        800,
        "下次怎么用：开头先抛出现代问题，再回到文物细节。",
    )
    assert "Campaign 总体经验包" in prompt
    assert "开头先抛出现代问题" in prompt


def test_experience_pack_filters_scope_and_platform(fresh_db):
    with Session(fresh_db) as session:
        ids = _seed_published(session)
        session.add(FeedbackExperience(
            brand_id=ids["brand_id"],
            campaign_id=ids["campaign_id"],
            platform="小红书",
            experience_type="写作经验",
            title="小红书开头要轻",
            action_advice="先给生活问题",
            performance_level="高表现",
        ))
        session.add(FeedbackExperience(
            brand_id=ids["brand_id"],
            campaign_id=None,
            platform="微信公众号",
            experience_type="写作经验",
            title="公众号信息密度",
            action_advice="保留史料细节",
            performance_level="中表现",
        ))
        session.commit()
        pack = experience_pack_text(session, ids["brand_id"], ids["campaign_id"], "写作经验", "小红书")

    assert "小红书开头要轻" in pack
    assert "公众号信息密度" not in pack
