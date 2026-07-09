"""④排期版纯逻辑：周容器、可排文章、排期与发布回填。"""
from datetime import date, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.modules.knowledge.models import Brand
from app.modules.schedule.models import ScheduleMetric, ScheduleSetting, ScheduleSlot, ScheduleWeek, _now
from app.modules.topic.models import Topic
from app.modules.writing.models import Article
from app.core.timezone import china_today

ACTIVE_SLOT_STATUSES = ("已排期", "已发布")
SCHEDULABLE_ARTICLE_STATUSES = ("已审核",)
RECOMMEND_TIMES = ("09:30", "12:30", "18:30")


def current_week(today: date | None = None) -> tuple[date, date]:
    today = today or china_today()
    start = today - timedelta(days=today.weekday())
    return start, start + timedelta(days=6)


def first_brand(session: Session) -> Brand | None:
    return session.exec(select(Brand).order_by(Brand.id)).first()


def get_schedule_settings(session: Session) -> ScheduleSetting:
    setting = session.get(ScheduleSetting, 1)
    if setting is None:
        setting = ScheduleSetting(id=1)
        session.add(setting)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            setting = session.get(ScheduleSetting, 1)
        else:
            session.refresh(setting)
    return setting


def save_recommend_prompt(session: Session, prompt: str) -> ScheduleSetting:
    setting = get_schedule_settings(session)
    setting.recommend_prompt = prompt.strip() or ScheduleSetting().recommend_prompt
    setting.updated_at = _now()
    session.add(setting)
    session.commit()
    session.refresh(setting)
    return setting


def weeks(session: Session, brand_id: int, campaign_id: int | None = None) -> list[ScheduleWeek]:
    return all_weeks(session, brand_id)


def all_weeks(session: Session, brand_id: int) -> list[ScheduleWeek]:
    return session.exec(
        select(ScheduleWeek).where(ScheduleWeek.brand_id == brand_id)
        .order_by(ScheduleWeek.week_start, ScheduleWeek.id)
    ).all()


def all_active_slots(session: Session, brand_id: int) -> list[ScheduleSlot]:
    return session.exec(
        select(ScheduleSlot)
        .where(ScheduleSlot.brand_id == brand_id, ScheduleSlot.status != "已取消")
        .order_by(ScheduleSlot.publish_date, ScheduleSlot.publish_time, ScheduleSlot.id)
    ).all()


def active_slots_for_articles(session: Session, article_ids: list[int]) -> dict[int, ScheduleSlot]:
    if not article_ids:
        return {}
    rows = session.exec(
        select(ScheduleSlot).where(
            ScheduleSlot.article_id.in_(article_ids),
            ScheduleSlot.status.in_(ACTIVE_SLOT_STATUSES),
        )
    ).all()
    return {slot.article_id: slot for slot in rows}


def _month_range(today: date | None = None) -> tuple[date, date]:
    today = today or china_today()
    start = today.replace(day=1)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def overview_stats(session: Session, brand_id: int, today: date | None = None) -> dict[str, int]:
    start, end = _month_range(today)
    slots = all_active_slots(session, brand_id)
    return {
        "month_scheduled": sum(start <= slot.publish_date < end for slot in slots),
        "month_published": sum(
            slot.status == "已发布"
            and start <= ((slot.published_at.date() if slot.published_at else slot.publish_date)) < end
            for slot in slots
        ),
        "scheduled_total": sum(slot.status == "已排期" for slot in slots),
        "published_total": sum(slot.status == "已发布" for slot in slots),
    }


def library_counts(session: Session, brand_id: int, campaigns: list) -> dict[str, int]:
    counts = {
        "all": len(library_articles(session, brand_id, include_all_campaigns=True)),
        "brand": len(library_articles(session, brand_id, None)),
    }
    for campaign in campaigns:
        counts[str(campaign.id)] = len(library_articles(session, brand_id, campaign.id))
    return counts


def next_week_dates(session: Session, brand_id: int, campaign_id: int | None = None) -> tuple[date, date]:
    existing = all_weeks(session, brand_id)
    if not existing:
        return current_week()
    latest = existing[-1]
    return latest.week_start + timedelta(days=7), latest.week_end + timedelta(days=7)


def add_week(session: Session, brand_id: int, campaign_id: int | None = None) -> ScheduleWeek:
    start, end = next_week_dates(session, brand_id, campaign_id)
    week = ScheduleWeek(brand_id=brand_id, campaign_id=None, week_start=start, week_end=end)
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


def metric_for_slot(session: Session, slot_id: int) -> ScheduleMetric | None:
    return session.exec(select(ScheduleMetric).where(ScheduleMetric.slot_id == slot_id)).first()


def metrics_for_slots(session: Session, slot_ids: list[int]) -> dict[int, ScheduleMetric]:
    if not slot_ids:
        return {}
    rows = session.exec(select(ScheduleMetric).where(ScheduleMetric.slot_id.in_(slot_ids))).all()
    return {m.slot_id: m for m in rows}


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


def library_articles(session: Session, brand_id: int, campaign_id: int | None = None,
                     include_all_campaigns: bool = False) -> list[Article]:
    q = select(Article).where(Article.status.in_(SCHEDULABLE_ARTICLE_STATUSES), Article.deleted_at == None)
    if not include_all_campaigns:
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
        raise ValueError("只有审核通过的文章可以排期")
    if active_slot_for_article(session, article_id):
        raise ValueError("文章已在排期表中")
    topic = session.get(Topic, article.topic_id)
    if topic is None or topic.brand_id != week.brand_id:
        raise ValueError("文章与排期周不属于同一品牌")
    day = publish_date or week.week_start
    if day < week.week_start or day > week.week_end:
        raise ValueError("发布日期不在该周范围内")
    slot = ScheduleSlot(
        week_id=week.id,
        article_id=article.id,
        topic_id=article.topic_id,
        brand_id=week.brand_id,
        campaign_id=topic.campaign_id,
        publish_date=day,
        publish_time=publish_time.strip(),
        platform=platform.strip(),
        notes=notes.strip(),
    )
    session.add(slot)
    session.commit()
    session.refresh(slot)
    return slot


def _open_position_in_week(session: Session, week: ScheduleWeek) -> tuple[date, str] | None:
    occupied = {
        (slot.publish_date, slot.publish_time)
        for slot in week_slots(session, week.id)
    }
    for offset in range(7):
        day = week.week_start + timedelta(days=offset)
        for time_value in RECOMMEND_TIMES:
            if (day, time_value) not in occupied:
                return day, time_value
    return None


def _balanced_open_position(session: Session, brand_id: int) -> tuple[ScheduleWeek, date, str]:
    scoped_weeks = all_weeks(session, brand_id)
    if not scoped_weeks:
        scoped_weeks = [add_week(session, brand_id)]
    while True:
        candidates: list[tuple[int, date, int, ScheduleWeek, date, str]] = []
        for week in scoped_weeks:
            slots = week_slots(session, week.id)
            open_position = _open_position_in_week(session, week)
            if open_position is None:
                continue
            day, time_value = open_position
            candidates.append((len(slots), week.week_start, week.id or 0, week, day, time_value))
        if candidates:
            _, _, _, week, day, time_value = min(candidates, key=lambda item: (item[0], item[1], item[2]))
            return week, day, time_value
        scoped_weeks.append(add_week(session, brand_id))


def recommend_slots(session: Session, brand_id: int,
                    campaign_id: int | None = None, prompt: str | None = None) -> list[ScheduleSlot]:
    """Heuristic schedule recommendation.

    ④ owns schedule placement, so this deliberately avoids mutating Article/Topic status.
    The prompt is persisted as the model-facing scheduling policy; the local fallback follows
    the same policy deterministically so every provider gets the same constraints.
    """
    created: list[ScheduleSlot] = []
    for article in all_schedulable_articles(session, brand_id):
        week, day, time_value = _balanced_open_position(session, brand_id)
        created.append(add_slot(
            session,
            week.id,
            article.id,
            day,
            time_value,
            "",
            "AI 推荐排期：按排期提示词均衡分配到已有 week。",
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


def save_metric(session: Session, slot_id: int, *, platform: str | None = None,
                published_url: str | None = None, published_at: datetime | None = None,
                wechat_read: int = 0,
                wechat_like: int = 0, wechat_share: int = 0,
                xhs_like: int = 0, xhs_comment: int = 0,
                xhs_collect: int = 0, notes: str = "") -> ScheduleMetric:
    slot = session.get(ScheduleSlot, slot_id)
    if slot is None:
        raise ValueError("排期不存在")
    if slot.status != "已发布":
        raise ValueError("未发布，不能填数据")
    if platform and platform.strip():
        slot.platform = platform.strip()
    if published_url is not None:
        slot.published_url = published_url.strip()
    if published_at is not None:
        slot.published_at = published_at
    slot.updated_at = _now()
    metric = metric_for_slot(session, slot_id)
    if metric is None:
        metric = ScheduleMetric(
            slot_id=slot.id,
            article_id=slot.article_id,
            topic_id=slot.topic_id,
            brand_id=slot.brand_id,
            campaign_id=slot.campaign_id,
        )
    metric.wechat_read = max(wechat_read, 0)
    metric.wechat_like = max(wechat_like, 0)
    metric.wechat_share = max(wechat_share, 0)
    metric.xhs_like = max(xhs_like, 0)
    metric.xhs_comment = max(xhs_comment, 0)
    metric.xhs_collect = max(xhs_collect, 0)
    metric.notes = notes.strip()
    metric.updated_at = _now()
    session.add(slot)
    session.add(metric)
    session.commit()
    session.refresh(metric)
    return metric
