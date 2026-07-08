"""④排期版纯逻辑：周容器、可排文章、排期与发布回填。"""
from datetime import date, datetime, timedelta

from sqlmodel import Session, select

from app.modules.knowledge.models import Brand
from app.modules.schedule.models import ScheduleSlot, ScheduleWeek, _now
from app.modules.topic.models import Topic
from app.modules.writing.models import Article

ACTIVE_SLOT_STATUSES = ("已排期", "已发布")
SCHEDULABLE_ARTICLE_STATUSES = ("待审核", "已生成")
RECOMMEND_TIMES = ("09:30", "12:30", "18:30")


def current_week(today: date | None = None) -> tuple[date, date]:
    today = today or date.today()
    start = today - timedelta(days=today.weekday())
    return start, start + timedelta(days=6)


def first_brand(session: Session) -> Brand | None:
    return session.exec(select(Brand).order_by(Brand.id)).first()


def weeks(session: Session, brand_id: int, campaign_id: int | None = None) -> list[ScheduleWeek]:
    q = select(ScheduleWeek).where(ScheduleWeek.brand_id == brand_id)
    if campaign_id is None:
        q = q.where(ScheduleWeek.campaign_id == None)
    else:
        q = q.where(ScheduleWeek.campaign_id == campaign_id)
    return session.exec(q.order_by(ScheduleWeek.week_start)).all()


def all_weeks(session: Session, brand_id: int) -> list[ScheduleWeek]:
    return session.exec(
        select(ScheduleWeek).where(ScheduleWeek.brand_id == brand_id)
        .order_by(ScheduleWeek.week_start, ScheduleWeek.campaign_id, ScheduleWeek.id)
    ).all()


def next_week_dates(session: Session, brand_id: int, campaign_id: int | None = None) -> tuple[date, date]:
    existing = weeks(session, brand_id, campaign_id)
    if not existing:
        return current_week()
    latest = existing[-1]
    return latest.week_start + timedelta(days=7), latest.week_end + timedelta(days=7)


def add_week(session: Session, brand_id: int, campaign_id: int | None = None) -> ScheduleWeek:
    start, end = next_week_dates(session, brand_id, campaign_id)
    week = ScheduleWeek(brand_id=brand_id, campaign_id=campaign_id, week_start=start, week_end=end)
    session.add(week)
    session.commit()
    session.refresh(week)
    return week


def week_slots(session: Session, week_id: int) -> list[ScheduleSlot]:
    return session.exec(
        select(ScheduleSlot)
        .where(ScheduleSlot.week_id == week_id, ScheduleSlot.status != "已取消")
        .order_by(ScheduleSlot.publish_date, ScheduleSlot.publish_time, ScheduleSlot.id)
    ).all()


def active_slot_for_article(session: Session, article_id: int) -> ScheduleSlot | None:
    return session.exec(
        select(ScheduleSlot).where(
            ScheduleSlot.article_id == article_id,
            ScheduleSlot.status.in_(ACTIVE_SLOT_STATUSES),
        )
    ).first()


def _is_schedulable_article(article: Article) -> bool:
    return article.status in SCHEDULABLE_ARTICLE_STATUSES and article.deleted_at is None


def schedulable_articles(session: Session, brand_id: int, campaign_id: int | None = None) -> list[Article]:
    q = select(Article).where(Article.status.in_(SCHEDULABLE_ARTICLE_STATUSES), Article.deleted_at == None)
    if campaign_id is None:
        q = q.where(Article.campaign_id == None)
    else:
        q = q.where(Article.campaign_id == campaign_id)
    rows = session.exec(q.order_by(Article.generated_at.desc(), Article.updated_at.desc(), Article.id.desc())).all()
    out: list[Article] = []
    for article in rows:
        topic = session.get(Topic, article.topic_id)
        if topic is None or topic.brand_id != brand_id:
            continue
        if active_slot_for_article(session, article.id):
            continue
        out.append(article)
    return out


def all_schedulable_articles(session: Session, brand_id: int) -> list[Article]:
    rows = session.exec(
        select(Article)
        .where(Article.status.in_(SCHEDULABLE_ARTICLE_STATUSES), Article.deleted_at == None)
        .order_by(Article.generated_at.desc(), Article.updated_at.desc(), Article.id.desc())
    ).all()
    out: list[Article] = []
    for article in rows:
        topic = session.get(Topic, article.topic_id)
        if topic is None or topic.brand_id != brand_id:
            continue
        if active_slot_for_article(session, article.id):
            continue
        out.append(article)
    return out


def add_slot(session: Session, week_id: int, article_id: int,
             publish_date: date | None = None, publish_time: str = "",
             platform: str = "", notes: str = "") -> ScheduleSlot:
    week = session.get(ScheduleWeek, week_id)
    if week is None:
        raise ValueError("周不存在")
    article = session.get(Article, article_id)
    if article is None:
        raise ValueError("文章不存在")
    if not _is_schedulable_article(article):
        raise ValueError("只有待审核文章可以排期")
    if active_slot_for_article(session, article_id):
        raise ValueError("文章已在排期表中")
    topic = session.get(Topic, article.topic_id)
    if topic is None or topic.brand_id != week.brand_id:
        raise ValueError("文章与排期周不属于同一品牌")
    if topic.campaign_id != week.campaign_id:
        raise ValueError("文章与排期周不属于同一活动范围")
    day = publish_date or week.week_start
    if day < week.week_start or day > week.week_end:
        raise ValueError("发布日期不在该周范围内")
    slot = ScheduleSlot(
        week_id=week.id,
        article_id=article.id,
        topic_id=article.topic_id,
        brand_id=week.brand_id,
        campaign_id=week.campaign_id,
        publish_date=day,
        publish_time=publish_time.strip(),
        platform=(platform or article.platform or "").strip(),
        notes=notes.strip(),
    )
    session.add(slot)
    session.commit()
    session.refresh(slot)
    return slot


def _first_open_position(session: Session, brand_id: int,
                         campaign_id: int | None) -> tuple[ScheduleWeek, date, str]:
    scoped_weeks = weeks(session, brand_id, campaign_id)
    if not scoped_weeks:
        scoped_weeks = [add_week(session, brand_id, campaign_id)]
    while True:
        occupied = {
            (slot.publish_date, slot.publish_time)
            for week in scoped_weeks
            for slot in week_slots(session, week.id)
        }
        for week in scoped_weeks:
            for offset in range(7):
                day = week.week_start + timedelta(days=offset)
                for time_value in RECOMMEND_TIMES:
                    if (day, time_value) not in occupied:
                        return week, day, time_value
        scoped_weeks.append(add_week(session, brand_id, campaign_id))


def recommend_slots(session: Session, brand_id: int,
                    campaign_id: int | None = None) -> list[ScheduleSlot]:
    """Heuristic schedule recommendation.

    ④ owns schedule placement, so this deliberately avoids mutating Article/Topic status.
    """
    created: list[ScheduleSlot] = []
    for article in schedulable_articles(session, brand_id, campaign_id):
        week, day, time_value = _first_open_position(session, brand_id, campaign_id)
        created.append(add_slot(
            session,
            week.id,
            article.id,
            day,
            time_value,
            article.platform,
            "AI 推荐排期：按待审核文章顺序与周内空闲时段自动放入。",
        ))
    return created


def move_slot(session: Session, slot_id: int, publish_date: date,
              publish_time: str = "") -> ScheduleSlot:
    slot = session.get(ScheduleSlot, slot_id)
    if slot is None:
        raise ValueError("排期不存在")
    if slot.status == "已发布":
        raise ValueError("已发布内容不可移动")
    week = session.get(ScheduleWeek, slot.week_id)
    if week is None or publish_date < week.week_start or publish_date > week.week_end:
        raise ValueError("发布日期不在该周范围内")
    slot.publish_date = publish_date
    slot.publish_time = publish_time.strip()
    slot.updated_at = _now()
    session.add(slot)
    session.commit()
    session.refresh(slot)
    return slot


def remove_slot(session: Session, slot_id: int) -> None:
    slot = session.get(ScheduleSlot, slot_id)
    if slot is None:
        return
    if slot.status == "已发布":
        raise ValueError("已发布内容不可移除，请先撤销发布")
    slot.status = "已取消"
    slot.updated_at = _now()
    session.add(slot)
    session.commit()


def delete_week(session: Session, week_id: int) -> None:
    week = session.get(ScheduleWeek, week_id)
    if week is None:
        return
    if week_slots(session, week_id):
        raise ValueError("非空周不可删除")
    session.delete(week)
    session.commit()


def publish_slot(session: Session, slot_id: int, platform: str = "",
                 published_url: str = "", published_at: datetime | None = None) -> ScheduleSlot:
    slot = session.get(ScheduleSlot, slot_id)
    if slot is None:
        raise ValueError("排期不存在")
    slot.status = "已发布"
    if platform.strip():
        slot.platform = platform.strip()
    slot.published_url = published_url.strip()
    slot.published_at = published_at or _now()
    slot.updated_at = _now()
    session.add(slot)
    session.commit()
    session.refresh(slot)
    return slot


def unpublish_slot(session: Session, slot_id: int) -> ScheduleSlot:
    slot = session.get(ScheduleSlot, slot_id)
    if slot is None:
        raise ValueError("排期不存在")
    slot.status = "已排期"
    slot.published_url = ""
    slot.published_at = None
    slot.updated_at = _now()
    session.add(slot)
    session.commit()
    session.refresh(slot)
    return slot
