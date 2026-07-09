"""Campaign 经验包：把⑤发布复盘汇总成①数据池可引用条目。"""
from dataclasses import dataclass

from sqlmodel import Session, select

from app.modules.feedback.models import FeedbackExperience
from app.modules.knowledge.models import Brand, Campaign, PoolTopic
from app.modules.schedule.models import ScheduleMetric, ScheduleSlot
from app.modules.writing.models import Article


@dataclass
class ExperiencePack:
    label: str
    pool_topic: PoolTopic
    article_count: int
    articles: list["ExperiencePackArticle"]


@dataclass
class ExperiencePackArticle:
    title: str
    platform: str
    performance_level: str
    metric_line: str
    entries: list[FeedbackExperience]
    source_slot_id: int | None = None
    article_id: int | None = None


def _metric_line(slot: ScheduleSlot | None, metric: ScheduleMetric | None) -> str:
    if slot is None:
        return "审核反馈：来自写作引擎人工审核，未经过发布数据。"
    if metric is None:
        return "发布数据：尚未回填"
    if "小红书" in (slot.platform or ""):
        return f"发布数据：小红书点赞 {metric.xhs_like}，评论 {metric.xhs_comment}，收藏 {metric.xhs_collect}"
    return f"发布数据：公众号阅读 {metric.wechat_read}，点赞 {metric.wechat_like}，转发 {metric.wechat_share}"


def _pack_title(label: str) -> str:
    return f"经验包｜{label}"


def _pack_content(session: Session, brand_id: int, label: str, entries: list[FeedbackExperience]) -> str:
    brand = session.get(Brand, brand_id)
    by_slot: dict[int | None, list[FeedbackExperience]] = {}
    for entry in entries:
        by_slot.setdefault(entry.source_slot_id, []).append(entry)
    lines = [
        f"【经验包】{label}",
        f"品牌：{brand.name if brand else brand_id}",
        f"来源：⑤数据反馈发布复盘 + ③写作引擎审核反馈，共 {len(by_slot)} 篇。",
        "",
        "用途：新 campaign 可引用本经验包，让历史发布反馈和审核退回原因迁移到新的选题和写作判断中。",
    ]
    for slot_id, slot_entries in by_slot.items():
        slot = session.get(ScheduleSlot, slot_id) if slot_id else None
        metric = session.exec(select(ScheduleMetric).where(ScheduleMetric.slot_id == slot_id)).first() if slot_id else None
        article = session.get(Article, slot.article_id) if slot and slot.article_id else None
        title = article.title if article else (slot_entries[0].title if slot_entries else "未知文章")
        platform = slot.platform if slot else (slot_entries[0].platform if slot_entries else "通用")
        lines.extend([
            "",
            f"## 文章：{title}",
            f"平台：{platform}；表现：{slot_entries[0].performance_level if slot_entries else '数据不足'}",
            _metric_line(slot, metric),
        ])
        for entry in sorted(slot_entries, key=lambda row: row.experience_type):
            lines.extend([
                f"- {entry.experience_type}：{entry.title}",
                f"  总结：{entry.summary}",
                f"  正向经验：{entry.positive_notes}",
                f"  反向风险：{entry.negative_notes}",
                f"  下次怎么用：{entry.action_advice}",
            ])
    return "\n".join(lines).strip()


def _active_entries(session: Session, brand_id: int, campaign_id: int | None) -> list[FeedbackExperience]:
    return session.exec(
        select(FeedbackExperience)
        .where(
            FeedbackExperience.brand_id == brand_id,
            FeedbackExperience.campaign_id == campaign_id,
            FeedbackExperience.is_active == True,
        )
        .order_by(FeedbackExperience.source_slot_id.desc(), FeedbackExperience.experience_type)
    ).all()


def _pack_articles(session: Session, entries: list[FeedbackExperience]) -> list[ExperiencePackArticle]:
    grouped: dict[str, list[FeedbackExperience]] = {}
    for entry in entries:
        if entry.source_slot_id:
            key = f"slot:{entry.source_slot_id}"
        elif entry.article_id:
            key = f"article:{entry.article_id}"
        else:
            key = "manual"
        grouped.setdefault(key, []).append(entry)

    articles: list[ExperiencePackArticle] = []
    for key, rows in grouped.items():
        first = rows[0]
        slot = session.get(ScheduleSlot, first.source_slot_id) if first.source_slot_id else None
        metric = session.exec(select(ScheduleMetric).where(ScheduleMetric.slot_id == first.source_slot_id)).first() if first.source_slot_id else None
        article = None
        if slot and slot.article_id:
            article = session.get(Article, slot.article_id)
        elif first.article_id:
            article = session.get(Article, first.article_id)
        title = article.title if article else first.title
        platform = slot.platform if slot else first.platform
        articles.append(ExperiencePackArticle(
            title=title,
            platform=platform or "通用",
            performance_level=first.performance_level or "数据不足",
            metric_line=_metric_line(slot, metric),
            entries=sorted(rows, key=lambda row: row.experience_type),
            source_slot_id=first.source_slot_id,
            article_id=(slot.article_id if slot else first.article_id),
        ))
    return articles


def sync_brand_experience_pack(session: Session, brand_id: int) -> PoolTopic | None:
    brand = session.get(Brand, brand_id)
    if brand is None:
        return None
    entries = _active_entries(session, brand_id, None)
    existing = session.exec(
        select(PoolTopic).where(
            PoolTopic.kind == "经验包",
            PoolTopic.source == "feedback",
            PoolTopic.source_campaign_id == None,
            PoolTopic.title == _pack_title("品牌常青"),
        )
    ).first()
    if not entries:
        if existing:
            existing.content = "暂无已总结的发布反馈。"
            session.add(existing)
            session.commit()
            session.refresh(existing)
        return existing
    topic = existing or PoolTopic(
        title=_pack_title("品牌常青"),
        kind="经验包",
        web_access=False,
        source="feedback",
        source_campaign_id=None,
        brand_tag=brand.name,
    )
    topic.title = _pack_title("品牌常青")
    topic.kind = "经验包"
    topic.web_access = False
    topic.source = "feedback"
    topic.source_campaign_id = None
    topic.brand_tag = brand.name
    topic.content = _pack_content(session, brand_id, "品牌常青", entries)
    session.add(topic)
    session.commit()
    session.refresh(topic)
    return topic


def sync_campaign_experience_pack(session: Session, campaign_id: int | None) -> PoolTopic | None:
    if campaign_id is None:
        return None
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        return None
    entries = _active_entries(session, campaign.brand_id, campaign_id)
    existing = session.exec(
        select(PoolTopic).where(
            PoolTopic.kind == "经验包",
            PoolTopic.source == "feedback",
            PoolTopic.source_campaign_id == campaign_id,
        )
    ).first()
    if not entries:
        if existing:
            existing.content = "暂无已总结的发布反馈。"
            session.add(existing)
            session.commit()
            session.refresh(existing)
        return existing
    brand = session.get(Brand, campaign.brand_id)
    topic = existing or PoolTopic(
        title=_pack_title(campaign.name),
        kind="经验包",
        web_access=False,
        source="feedback",
        source_campaign_id=campaign_id,
        brand_tag=brand.name if brand else None,
    )
    topic.title = _pack_title(campaign.name)
    topic.kind = "经验包"
    topic.web_access = False
    topic.source = "feedback"
    topic.source_campaign_id = campaign_id
    topic.brand_tag = brand.name if brand else topic.brand_tag
    topic.content = _pack_content(session, campaign.brand_id, campaign.name, entries)
    session.add(topic)
    session.commit()
    session.refresh(topic)
    return topic


def sync_all_campaign_experience_packs(session: Session, brand_id: int | None = None) -> list[ExperiencePack]:
    campaigns = session.exec(
        select(Campaign)
        .where(Campaign.brand_id == brand_id if brand_id is not None else True)
        .order_by(Campaign.id.desc())
    ).all()
    packs: list[ExperiencePack] = []
    if brand_id is not None:
        brand_topic = sync_brand_experience_pack(session, brand_id)
        if brand_topic is not None and brand_topic.content != "暂无已总结的发布反馈。":
            brand_entries = _active_entries(session, brand_id, None)
            if brand_entries:
                articles = _pack_articles(session, brand_entries)
                packs.append(ExperiencePack(
                    label="品牌常青",
                    pool_topic=brand_topic,
                    article_count=len(articles),
                    articles=articles,
                ))
    for campaign in campaigns:
        topic = sync_campaign_experience_pack(session, campaign.id)
        if topic is None or topic.source_campaign_id is None:
            continue
        entries = _active_entries(session, campaign.brand_id, campaign.id)
        if not entries:
            continue
        articles = _pack_articles(session, entries)
        packs.append(ExperiencePack(
            label=campaign.name,
            pool_topic=topic,
            article_count=len(articles),
            articles=articles,
        ))
    return packs


def campaign_experience_pack_options(session: Session, brand_id: int) -> list[PoolTopic]:
    sync_all_campaign_experience_packs(session, brand_id)
    return session.exec(
        select(PoolTopic)
        .where(
            PoolTopic.kind == "经验包",
            PoolTopic.source == "feedback",
        )
        .order_by(PoolTopic.id.desc())
    ).all()
