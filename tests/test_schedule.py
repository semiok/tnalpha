"""④排期版：文章池、排期状态和发布回填。"""
from datetime import datetime

from sqlmodel import Session, select

from app.modules.knowledge.models import Brand, Campaign
from app.modules.schedule import schedule
from app.modules.schedule.models import ScheduleSlot
from app.modules.topic.models import Topic
from app.modules.writing.models import Article


def _seed(session: Session) -> dict[str, int]:
    brand = Brand(name="敦煌IP")
    session.add(brand)
    session.commit()
    session.refresh(brand)

    campaign = Campaign(brand_id=brand.id, name="丝路有多长")
    session.add(campaign)
    session.commit()
    session.refresh(campaign)

    brand_topic = Topic(brand_id=brand.id, campaign_id=None, title="品牌常青选题", status="采纳")
    campaign_topic = Topic(brand_id=brand.id, campaign_id=campaign.id, title="活动选题", status="采纳")
    draft_topic = Topic(brand_id=brand.id, campaign_id=campaign.id, title="未完成选题", status="采纳")
    session.add(brand_topic)
    session.add(campaign_topic)
    session.add(draft_topic)
    session.commit()
    session.refresh(brand_topic)
    session.refresh(campaign_topic)
    session.refresh(draft_topic)

    generated_at = datetime(2026, 7, 7, 9, 0)
    brand_article = Article(
        topic_id=brand_topic.id,
        campaign_id=None,
        title="品牌文章",
        body="品牌正文",
        image_prompt="品牌图像提示词",
        status="待审核",
        generated_at=generated_at,
        platform="小红书",
        word_count=800,
    )
    campaign_article = Article(
        topic_id=campaign_topic.id,
        campaign_id=campaign.id,
        title="活动文章",
        body="活动正文",
        image_prompt="活动图像提示词",
        status="待审核",
        generated_at=generated_at,
        platform="微信公众号",
        word_count=1200,
    )
    draft_article = Article(
        topic_id=draft_topic.id,
        campaign_id=campaign.id,
        title="待配图文章",
        status="待配图",
        generated_at=generated_at,
    )
    session.add(brand_article)
    session.add(campaign_article)
    session.add(draft_article)
    session.commit()
    session.refresh(brand_article)
    session.refresh(campaign_article)
    session.refresh(draft_article)

    return {
        "brand_id": brand.id,
        "campaign_id": campaign.id,
        "brand_article_id": brand_article.id,
        "campaign_article_id": campaign_article.id,
        "draft_article_id": draft_article.id,
    }


def test_schedulable_articles_are_generated_and_unscheduled(fresh_db):
    with Session(fresh_db) as session:
        ids = _seed(session)
        week = schedule.add_week(session, ids["brand_id"], ids["campaign_id"])
        slot = schedule.add_slot(session, week.id, ids["campaign_article_id"], week.week_start)

        campaign_articles = schedule.schedulable_articles(session, ids["brand_id"], ids["campaign_id"])
        assert [a.id for a in campaign_articles] == []
        assert schedule.schedulable_articles(session, ids["brand_id"], None)[0].id == ids["brand_article_id"]
        assert session.get(Article, ids["campaign_article_id"]).status == "待审核"
        assert session.get(ScheduleSlot, slot.id).status == "已排期"


def test_schedule_page_lists_generated_articles_and_preview(owner_client, fresh_db):
    with Session(fresh_db) as session:
        ids = _seed(session)

    r = owner_client.get(f"/schedule?campaign_id={ids['campaign_id']}")
    assert r.status_code == 200
    assert "活动文章" in r.text
    assert "待配图文章" not in r.text

    preview = owner_client.get(f"/schedule/articles/{ids['campaign_article_id']}/preview")
    assert preview.status_code == 200
    assert "活动正文" in preview.text
    assert "活动图像提示词" in preview.text


def test_editor_can_schedule_and_publisher_can_publish(owner_client, publisher_client, fresh_db):
    with Session(fresh_db) as session:
        ids = _seed(session)
        week = schedule.add_week(session, ids["brand_id"], ids["campaign_id"])
        publish_date = week.week_start
        week_id = week.id

    blocked = publisher_client.post(
        "/schedule/slots/add",
        data={"week_id": week_id, "article_id": ids["campaign_article_id"], "publish_date": publish_date.isoformat()},
        follow_redirects=False,
    )
    assert blocked.status_code == 403

    added = owner_client.post(
        "/schedule/slots/add",
        data={
            "week_id": week_id,
            "article_id": ids["campaign_article_id"],
            "publish_date": publish_date.isoformat(),
            "publish_time": "09:30",
        },
        follow_redirects=False,
    )
    assert added.status_code == 303

    page = owner_client.get(f"/schedule?campaign_id={ids['campaign_id']}")
    assert "活动文章" in page.text
    assert "09:30" in page.text

    with Session(fresh_db) as session:
        slot = session.exec(select(ScheduleSlot)).one()
        slot_id = slot.id

    published = publisher_client.post(
        f"/schedule/slots/{slot_id}/publish",
        data={
            "platform": "微信公众号",
            "published_url": "https://example.com/post",
            "published_at": "2026-07-08T10:00",
        },
        follow_redirects=False,
    )
    assert published.status_code == 303

    with Session(fresh_db) as session:
        slot = session.get(ScheduleSlot, slot_id)
        assert slot.status == "已发布"
        assert slot.published_url == "https://example.com/post"
        assert slot.published_at == datetime(2026, 7, 8, 10, 0)


def test_recommend_schedule_creates_week_and_slots(owner_client, fresh_db):
    with Session(fresh_db) as session:
        ids = _seed(session)

    recommended = owner_client.post(
        "/schedule/recommend",
        data={"campaign_id": str(ids["campaign_id"])},
        follow_redirects=False,
    )
    assert recommended.status_code == 303

    with Session(fresh_db) as session:
        slots = session.exec(select(ScheduleSlot)).all()
        assert len(slots) == 1
        assert slots[0].article_id == ids["campaign_article_id"]
        assert slots[0].publish_time == "09:30"
        assert "AI 推荐排期" in slots[0].notes
        assert session.get(Article, ids["campaign_article_id"]).status == "待审核"


def test_remove_slot_keeps_article_generated(fresh_db):
    with Session(fresh_db) as session:
        ids = _seed(session)
        week = schedule.add_week(session, ids["brand_id"], ids["campaign_id"])
        slot = schedule.add_slot(session, week.id, ids["campaign_article_id"], week.week_start)

        schedule.remove_slot(session, slot.id)

        assert session.get(ScheduleSlot, slot.id).status == "已取消"
        assert session.get(Article, ids["campaign_article_id"]).status == "待审核"
        assert schedule.schedulable_articles(session, ids["brand_id"], ids["campaign_id"])[0].id == ids["campaign_article_id"]
