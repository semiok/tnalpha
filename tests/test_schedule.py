"""④排期版：文章池、排期状态和发布回填。"""
from datetime import date, datetime, timedelta

from sqlmodel import Session, select

from app.modules.knowledge.models import Brand, Campaign
from app.modules.schedule import schedule
from app.modules.schedule.models import ScheduleMetric, ScheduleSetting, ScheduleSlot, ScheduleWeek
from app.modules.topic.models import Topic
from app.modules.writing.models import Article


def test_current_week_uses_china_today_by_default(monkeypatch):
    monkeypatch.setattr(schedule, "china_today", lambda: date(2026, 7, 13))
    assert schedule.current_week() == (date(2026, 7, 13), date(2026, 7, 19))


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
        status="已审核",
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
        status="已审核",
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
        assert session.get(Article, ids["campaign_article_id"]).status == "已审核"
        assert session.get(ScheduleSlot, slot.id).status == "已排期"
        assert session.get(ScheduleWeek, week.id).campaign_id is None
        assert slot.campaign_id == ids["campaign_id"]
        assert slot.platform == ""


def test_week_accepts_articles_from_multiple_campaign_scopes(fresh_db):
    with Session(fresh_db) as session:
        ids = _seed(session)
        week = schedule.add_week(session, ids["brand_id"])
        brand_slot = schedule.add_slot(session, week.id, ids["brand_article_id"], week.week_start)
        campaign_slot = schedule.add_slot(session, week.id, ids["campaign_article_id"], week.week_start)

        assert brand_slot.week_id == campaign_slot.week_id == week.id
        assert brand_slot.campaign_id is None
        assert campaign_slot.campaign_id == ids["campaign_id"]


def test_schedule_page_lists_generated_articles_and_preview(owner_client, fresh_db):
    with Session(fresh_db) as session:
        ids = _seed(session)
        schedule.add_week(session, ids["brand_id"], ids["campaign_id"])

    home = owner_client.get("/schedule")
    assert home.status_code == 200
    assert "排期数据" in home.text
    assert "排期库" in home.text
    assert "/schedule/weeks/" in home.text
    assert "/pick?return_to=/schedule" in home.text
    assert "去排期库指定" not in home.text

    r = owner_client.get(f"/schedule/library?campaign_id={ids['campaign_id']}")
    assert r.status_code == 200
    assert "活动文章" in r.text
    assert "待配图文章" not in r.text
    assert "指定发布周" in r.text or "该范围还没有 week" in r.text

    preview = owner_client.get(f"/schedule/articles/{ids['campaign_article_id']}/preview")
    assert preview.status_code == 200
    assert "活动正文" in preview.text
    assert "活动图像提示词" in preview.text


def test_schedule_weeks_show_latest_first_and_nearest_last(owner_client, fresh_db):
    with Session(fresh_db) as session:
        ids = _seed(session)
        weeks = [schedule.add_week(session, ids["brand_id"]) for _ in range(3)]
        labels = [f"{w.week_start.strftime('%m.%d')}-{w.week_end.strftime('%m.%d')}" for w in weeks]

    page = owner_client.get("/schedule")
    assert page.status_code == 200
    positions = [page.text.find(label) for label in labels]
    assert all(pos >= 0 for pos in positions)
    assert positions[2] < positions[1] < positions[0]


def test_pick_article_modal_adds_without_library_jump(owner_client, fresh_db):
    with Session(fresh_db) as session:
        ids = _seed(session)
        week = schedule.add_week(session, ids["brand_id"], ids["campaign_id"])
        week_id = week.id

    modal = owner_client.get(f"/schedule/weeks/{week_id}/pick?return_to=/schedule?campaign_id={ids['campaign_id']}")
    assert modal.status_code == 200
    assert "新增文章" in modal.text
    assert "活动文章" in modal.text
    assert "品牌文章" in modal.text
    assert "推荐平台：微信公众号" in modal.text
    assert "/schedule/slots/add" in modal.text
    assert f'value="/schedule?campaign_id={ids["campaign_id"]}"' in modal.text


def test_publisher_can_schedule_and_publish(owner_client, publisher_client, fresh_db):
    with Session(fresh_db) as session:
        ids = _seed(session)
        week = schedule.add_week(session, ids["brand_id"], ids["campaign_id"])
        publish_date = week.week_start
        week_id = week.id

    added = publisher_client.post(
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
    assert "推荐平台：微信公众号" in page.text

    with Session(fresh_db) as session:
        slot = session.exec(select(ScheduleSlot)).one()
        slot_id = slot.id
    assert f"/schedule/slots/{slot_id}/publish-modal" in page.text

    modal = publisher_client.get(f"/schedule/slots/{slot_id}/publish-modal?return_to=/schedule?campaign_id={ids['campaign_id']}")
    assert modal.status_code == 200
    assert "发布平台" in modal.text
    assert "推荐平台：微信公众号" in modal.text
    assert '<option value="微信公众号" selected>微信公众号</option>' in modal.text
    assert "发布时间" in modal.text
    assert f'value="{publish_date.isoformat()}T09:30"' in modal.text
    assert "发布链接" in modal.text
    assert f'value="/schedule?campaign_id={ids["campaign_id"]}"' in modal.text

    published = publisher_client.post(
        f"/schedule/slots/{slot_id}/publish",
        data={
            "platform": "微信公众号",
            "published_url": "https://example.com/post",
            "published_at": "2026-07-08T10:00",
            "return_to": f"/schedule?campaign_id={ids['campaign_id']}",
        },
        follow_redirects=False,
    )
    assert published.status_code == 303
    assert published.headers["location"] == f"/schedule?campaign_id={ids['campaign_id']}"

    with Session(fresh_db) as session:
        slot = session.get(ScheduleSlot, slot_id)
        assert slot.status == "已发布"
        assert slot.platform == "微信公众号"
        assert slot.published_url == "https://example.com/post"
        assert slot.published_at == datetime(2026, 7, 8, 10, 0)


def test_move_slot_highlights_save_button_and_confirms_update(owner_client, fresh_db):
    with Session(fresh_db) as session:
        ids = _seed(session)
        week = schedule.add_week(session, ids["brand_id"], ids["campaign_id"])
        slot = schedule.add_slot(session, week.id, ids["campaign_article_id"], week.week_start, "09:30")
        slot_id = slot.id
        next_day = week.week_start + timedelta(days=1)

    page = owner_client.get("/schedule")
    assert page.status_code == 200
    assert "保存时间" in page.text
    assert "x-data='{initialDate:" in page.text
    assert "get changed()" in page.text
    assert "bg-brand-600 text-white" in page.text
    assert 'name="return_to" value="/schedule?time_updated=1"' in page.text

    moved = owner_client.post(
        f"/schedule/slots/{slot_id}/move",
        data={
            "publish_date": next_day.isoformat(),
            "publish_time": "10:15",
            "return_to": "/schedule?time_updated=1",
        },
        follow_redirects=False,
    )
    assert moved.status_code == 303
    assert moved.headers["location"] == "/schedule?time_updated=1"

    updated_page = owner_client.get("/schedule?time_updated=1")
    assert updated_page.status_code == 200
    assert "时间已修改" in updated_page.text
    assert "10:15" in updated_page.text

    with Session(fresh_db) as session:
        slot = session.get(ScheduleSlot, slot_id)
        assert slot.publish_date == next_day
        assert slot.publish_time == "10:15"


def test_library_can_assign_article_to_week(owner_client, fresh_db):
    with Session(fresh_db) as session:
        ids = _seed(session)
        week = schedule.add_week(session, ids["brand_id"], ids["campaign_id"])
        week_id = week.id
        publish_date = week.week_start

    page = owner_client.get(f"/schedule/library?campaign_id={ids['campaign_id']}&article_id={ids['campaign_article_id']}")
    assert page.status_code == 200
    assert "活动文章" in page.text
    assert "指定发布周" in page.text
    assert f"/schedule/articles/{ids['campaign_article_id']}/assign-modal" in page.text
    assert 'name="week_id"' not in page.text

    modal = owner_client.get(
        f"/schedule/articles/{ids['campaign_article_id']}/assign-modal"
        f"?return_to=/schedule/library?campaign_id={ids['campaign_id']}"
    )
    assert modal.status_code == 200
    assert "指定发布周" in modal.text
    assert "活动文章" in modal.text
    assert "/schedule/slots/add" in modal.text
    assert 'name="week_id"' in modal.text
    assert f'value="{week_id}"' in modal.text
    assert f'publishDate: "{publish_date.isoformat()}"' in modal.text

    assigned = owner_client.post(
        "/schedule/slots/add",
        data={
            "week_id": week_id,
            "article_id": ids["campaign_article_id"],
            "publish_date": publish_date.isoformat(),
            "return_to": f"/schedule/library?campaign_id={ids['campaign_id']}&article_id={ids['campaign_article_id']}",
        },
        follow_redirects=False,
    )
    assert assigned.status_code == 303
    assert assigned.headers["location"].startswith("/schedule/library")

    with Session(fresh_db) as session:
        slot = session.exec(select(ScheduleSlot)).one()
        assert slot.article_id == ids["campaign_article_id"]
        assert slot.week_id == week_id


def test_library_filters_by_status_then_campaign(owner_client, fresh_db):
    with Session(fresh_db) as session:
        ids = _seed(session)
        extra_topic = Topic(
            brand_id=ids["brand_id"],
            campaign_id=ids["campaign_id"],
            title="额外活动选题",
            status="采纳",
        )
        session.add(extra_topic)
        session.commit()
        session.refresh(extra_topic)
        extra_article = Article(
            topic_id=extra_topic.id,
            campaign_id=ids["campaign_id"],
            title="额外可排期文章",
            body="正文",
            status="已审核",
            generated_at=datetime(2026, 7, 7, 10, 0),
            platform="小红书",
        )
        session.add(extra_article)
        session.commit()
        session.refresh(extra_article)
        week = schedule.add_week(session, ids["brand_id"])
        scheduled_slot = schedule.add_slot(session, week.id, ids["campaign_article_id"], week.week_start, "09:30")
        published_slot = schedule.add_slot(session, week.id, ids["brand_article_id"], week.week_start, "12:30")
        schedule.publish_slot(session, published_slot.id, "小红书", "", datetime(2026, 7, 8, 10, 0))
        campaign_id = ids["campaign_id"]

    all_page = owner_client.get("/schedule/library")
    assert all_page.status_code == 200
    assert "全部 " in all_page.text
    assert "可排期 " in all_page.text
    assert "已排期 " in all_page.text
    assert "已发布 " in all_page.text
    assert "全部范围" in all_page.text
    assert "品牌常青" in all_page.text
    assert "丝路有多长" in all_page.text

    schedulable = owner_client.get("/schedule/library?status=可排期&campaign_id=all")
    assert "额外可排期文章" in schedulable.text
    assert "活动文章" not in schedulable.text
    assert "品牌文章" not in schedulable.text

    scheduled = owner_client.get(f"/schedule/library?status=已排期&campaign_id={campaign_id}")
    assert "活动文章" in scheduled.text
    assert "额外可排期文章" not in scheduled.text
    assert "品牌文章" not in scheduled.text

    published = owner_client.get("/schedule/library?status=已发布&campaign_id=brand")
    assert "品牌文章" in published.text
    assert "活动文章" not in published.text
    assert "已发布于" in published.text


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
        assert len(slots) == 2
        assert {slot.article_id for slot in slots} == {ids["brand_article_id"], ids["campaign_article_id"]}
        assert [slot.publish_time for slot in slots] == ["09:30", "12:30"]
        assert all("AI 推荐排期" in slot.notes for slot in slots)
        assert all(slot.platform == "" for slot in slots)
        assert session.get(Article, ids["campaign_article_id"]).status == "已审核"
        assert len(session.exec(select(ScheduleWeek)).all()) == 1


def test_recommend_schedule_balances_across_existing_weeks(owner_client, fresh_db):
    with Session(fresh_db) as session:
        ids = _seed(session)
        for idx in range(2):
            topic = Topic(brand_id=ids["brand_id"], campaign_id=ids["campaign_id"], title=f"补充选题{idx}", status="采纳")
            session.add(topic)
            session.commit()
            session.refresh(topic)
            session.add(Article(
                topic_id=topic.id,
                campaign_id=ids["campaign_id"],
                title=f"补充文章{idx}",
                body="正文",
                status="已审核",
                generated_at=datetime(2026, 7, 7, 9, idx),
            ))
            session.commit()
        week_ids = [schedule.add_week(session, ids["brand_id"]).id for _ in range(4)]

    recommended = owner_client.post("/schedule/recommend", follow_redirects=False)
    assert recommended.status_code == 303

    with Session(fresh_db) as session:
        slots = session.exec(
            select(ScheduleSlot).where(ScheduleSlot.status == "已排期").order_by(ScheduleSlot.week_id)
        ).all()
        assert len(slots) == 4
        assert {slot.week_id for slot in slots} == set(week_ids)
        assert all(slot.notes == "AI 推荐排期：按排期提示词均衡分配到已有 week。" for slot in slots)


def test_schedule_recommend_prompt_can_be_saved(owner_client, fresh_db):
    with Session(fresh_db) as session:
        ids = _seed(session)
        schedule.add_week(session, ids["brand_id"])

    page = owner_client.get("/schedule")
    assert page.status_code == 200
    assert "AI 推荐提示词" in page.text
    assert "不能把所有内容集中塞进第一周" in page.text

    saved = owner_client.post(
        "/schedule/settings/recommend-prompt",
        data={"recommend_prompt": "每个 week 都要优先获得一篇推荐。"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert saved.headers["location"] == "/schedule"

    with Session(fresh_db) as session:
        setting = session.get(ScheduleSetting, 1)
        assert setting.recommend_prompt == "每个 week 都要优先获得一篇推荐。"

    page = owner_client.get("/schedule")
    assert "每个 week 都要优先获得一篇推荐。" in page.text


def test_metrics_can_only_be_filled_after_publish(owner_client, publisher_client, fresh_db):
    with Session(fresh_db) as session:
        ids = _seed(session)
        week = schedule.add_week(session, ids["brand_id"], ids["campaign_id"])
        slot = schedule.add_slot(session, week.id, ids["campaign_article_id"], week.week_start)
        slot_id = slot.id

    blocked = publisher_client.post(
        f"/schedule/slots/{slot_id}/metrics",
        data={"wechat_read": "100"},
        follow_redirects=False,
    )
    assert blocked.status_code == 400

    owner_client.post(
        f"/schedule/slots/{slot_id}/publish",
        data={
            "platform": "微信公众号",
            "published_url": "https://example.com/post",
            "published_at": "2026-07-08T11:00",
        },
        follow_redirects=False,
    )
    saved = publisher_client.post(
        f"/schedule/slots/{slot_id}/metrics",
        data={
            "wechat_read": "1200",
            "wechat_like": "88",
            "wechat_share": "12",
            "notes": "首日数据",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303

    with Session(fresh_db) as session:
        metric = session.exec(select(ScheduleMetric)).one()
        assert metric.slot_id == slot_id
        assert metric.wechat_read == 1200
        assert metric.wechat_like == 88
        assert metric.xhs_comment == 0
        assert metric.notes == "首日数据"
        slot = session.get(ScheduleSlot, slot_id)
        assert slot.platform == "微信公众号"
        assert slot.published_url == "https://example.com/post"
        assert slot.published_at == datetime(2026, 7, 8, 11, 0)

    page = owner_client.get(f"/schedule?campaign_id={ids['campaign_id']}")
    assert "公众号：阅读 1200 点赞 88 转发 12" in page.text
    assert "小红书：点赞" not in page.text
    assert 'name="platform"' not in page.text
    assert 'name="published_at"' not in page.text
    assert 'name="published_url"' not in page.text
    assert "首日数据" in page.text


def test_xhs_feedback_only_shows_xhs_fields(owner_client, publisher_client, fresh_db):
    with Session(fresh_db) as session:
        ids = _seed(session)
        week = schedule.add_week(session, ids["brand_id"])
        slot = schedule.add_slot(session, week.id, ids["brand_article_id"], week.week_start)
        slot_id = slot.id

    owner_client.post(
        f"/schedule/slots/{slot_id}/publish",
        data={"platform": "小红书", "published_at": "2026-07-08T11:00"},
        follow_redirects=False,
    )
    saved = publisher_client.post(
        f"/schedule/slots/{slot_id}/metrics",
        data={"xhs_like": "66", "xhs_comment": "9", "xhs_collect": "31"},
        follow_redirects=False,
    )
    assert saved.status_code == 303

    page = owner_client.get("/schedule")
    assert "小红书：点赞 66 评论 9 收藏 31" in page.text
    assert "公众号：阅读" not in page.text
    assert "小红书点赞" in page.text
    assert "公众号阅读" not in page.text


def test_remove_slot_keeps_article_generated(fresh_db):
    with Session(fresh_db) as session:
        ids = _seed(session)
        week = schedule.add_week(session, ids["brand_id"], ids["campaign_id"])
        slot = schedule.add_slot(session, week.id, ids["campaign_article_id"], week.week_start)

        schedule.remove_slot(session, slot.id)

        assert session.get(ScheduleSlot, slot.id).status == "已取消"
        assert session.get(Article, ids["campaign_article_id"]).status == "已审核"
        assert schedule.schedulable_articles(session, ids["brand_id"], ids["campaign_id"])[0].id == ids["campaign_article_id"]
