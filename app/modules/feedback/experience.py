"""⑤数据反馈经验包服务：发布数据 → 经验条目 → ②/③引用文本。"""
from dataclasses import dataclass

from sqlmodel import Session, select

from app.core import llm
from app.modules.feedback.models import EXPERIENCE_TYPES, FeedbackExperience, _now
from app.modules.knowledge.models import Campaign
from app.modules.schedule.models import ScheduleMetric, ScheduleSlot
from app.modules.topic.models import Topic
from app.modules.writing.debate import clean_llm_output
from app.modules.writing.models import Article


@dataclass
class PublishedSample:
    slot: ScheduleSlot
    metric: ScheduleMetric | None
    article: Article | None
    topic: Topic | None
    campaign_name: str
    performance_level: str
    score: int


def _metric_score(platform: str, metric: ScheduleMetric | None) -> int:
    if metric is None:
        return 0
    if "小红书" in (platform or ""):
        return metric.xhs_like + metric.xhs_comment * 3 + metric.xhs_collect * 2
    return metric.wechat_read + metric.wechat_like * 5 + metric.wechat_share * 10


def performance_level(platform: str, metric: ScheduleMetric | None) -> str:
    score = _metric_score(platform, metric)
    if metric is None or score <= 0:
        return "数据不足"
    if "小红书" in (platform or ""):
        if score >= 100:
            return "高表现"
        if score >= 20:
            return "中表现"
        return "低表现"
    if score >= 1000:
        return "高表现"
    if score >= 200:
        return "中表现"
    return "低表现"


def published_samples(session: Session, brand_id: int) -> list[PublishedSample]:
    slots = session.exec(
        select(ScheduleSlot)
        .where(ScheduleSlot.brand_id == brand_id, ScheduleSlot.status == "已发布")
        .order_by(ScheduleSlot.published_at.desc(), ScheduleSlot.publish_date.desc(), ScheduleSlot.id.desc())
    ).all()
    campaign_ids = {slot.campaign_id for slot in slots if slot.campaign_id is not None}
    campaigns = {
        c.id: c.name for c in session.exec(select(Campaign).where(Campaign.id.in_(campaign_ids))).all()
    } if campaign_ids else {}
    out: list[PublishedSample] = []
    for slot in slots:
        metric = session.exec(select(ScheduleMetric).where(ScheduleMetric.slot_id == slot.id)).first()
        article = session.get(Article, slot.article_id)
        topic = session.get(Topic, slot.topic_id)
        level = performance_level(slot.platform, metric)
        out.append(PublishedSample(
            slot=slot,
            metric=metric,
            article=article,
            topic=topic,
            campaign_name=campaigns.get(slot.campaign_id, "品牌常青"),
            performance_level=level,
            score=_metric_score(slot.platform, metric),
        ))
    return out


def experiences_by_slot(session: Session, slot_ids: list[int]) -> dict[int, list[FeedbackExperience]]:
    if not slot_ids:
        return {}
    rows = session.exec(
        select(FeedbackExperience)
        .where(
            FeedbackExperience.source_slot_id.in_(slot_ids),
            FeedbackExperience.is_active == True,
        )
        .order_by(FeedbackExperience.experience_type, FeedbackExperience.updated_at.desc(), FeedbackExperience.id.desc())
    ).all()
    grouped: dict[int, list[FeedbackExperience]] = {}
    for row in rows:
        if row.source_slot_id is None:
            continue
        grouped.setdefault(row.source_slot_id, []).append(row)
    return grouped


def sample_status(entries: list[FeedbackExperience] | None) -> str:
    types = {entry.experience_type for entry in (entries or []) if entry.is_active}
    return "已总结" if set(EXPERIENCE_TYPES).issubset(types) else "待总结"


def _metrics_text(slot: ScheduleSlot, metric: ScheduleMetric | None) -> str:
    if metric is None:
        return "尚未回填媒体数据"
    if "小红书" in (slot.platform or ""):
        return f"小红书：点赞 {metric.xhs_like}，评论 {metric.xhs_comment}，收藏 {metric.xhs_collect}。备注：{metric.notes or '无'}"
    return f"公众号：阅读 {metric.wechat_read}，点赞 {metric.wechat_like}，转发 {metric.wechat_share}。备注：{metric.notes or '无'}"


def _fallback_experience(sample: PublishedSample, experience_type: str) -> dict[str, str]:
    title = sample.article.title if sample.article else (sample.topic.title if sample.topic else "发布样本")
    if experience_type == "选题经验":
        action = "后续选题优先保留具体物件、明确问题和可视化素材，避免只做抽象概念复述。"
    else:
        action = "后续写作优先强化开头钩子、段落节奏和平台语气，并在结尾留下可评论的问题。"
    return {
        "title": f"{sample.performance_level}复盘：{title}"[:120],
        "summary": f"基于《{title}》的发布数据沉淀，平台为 {sample.slot.platform or '未填写'}，表现判断为{sample.performance_level}。",
        "positive_notes": "保留已有内容中具体、可感、可转述的部分。",
        "negative_notes": "避免重复弱反馈的抽象表达或素材堆砌。",
        "action_advice": action,
    }


def _draft_prompt(sample: PublishedSample, experience_type: str) -> str:
    article = sample.article
    topic = sample.topic
    body = (article.body if article else "")[:2400]
    return f"""你是内容复盘负责人。请根据发布样本沉淀一条「{experience_type}」。

【发布样本】
标题：{article.title if article else (topic.title if topic else "未知")}
来源选题：{topic.title if topic else "未知"}
选题纲要：{topic.outline if topic else ""}
Campaign：{sample.campaign_name}
发布平台：{sample.slot.platform or "未填写"}
发布时间：{sample.slot.published_at or sample.slot.publish_date}
表现判断：{sample.performance_level}
媒体数据：{_metrics_text(sample.slot, sample.metric)}

【文章正文节选】
{body or "（无正文）"}

请输出一条可复用经验，严格按以下格式，不要输出思考过程：
标题：一句话概括经验
总结：这条经验说明什么
正向经验：以后应保留或放大的做法
反向风险：以后应避免的问题
下次怎么用：给②选题库或③写作引擎的具体指令
"""


def _parse_field(text: str, label: str) -> str:
    import re
    labels = "标题|总结|正向经验|反向风险|下次怎么用"
    m = re.search(rf"{label}[:：]\s*(.+?)(?=\n\s*(?:{labels})[:：]|$)", text or "", flags=re.S)
    return m.group(1).strip() if m else ""


def build_experience_draft(session: Session, slot_id: int, experience_type: str) -> dict[str, str]:
    sample = next((s for s in published_samples(session, _slot_brand_id(session, slot_id)) if s.slot.id == slot_id), None)
    if sample is None:
        raise ValueError("发布样本不存在")
    fallback = _fallback_experience(sample, experience_type)
    llm_provider, llm_model = llm.text_model_info("feedback")
    fallback.update({"llm_provider": llm_provider, "llm_model": llm_model})
    try:
        raw = clean_llm_output(llm.generate_text(
            _draft_prompt(sample, experience_type),
            task="feedback_experience",
            module="feedback",
            fallback=False,
        ))
    except RuntimeError:
        return fallback
    parsed = {
        "title": _parse_field(raw, "标题") or fallback["title"],
        "summary": _parse_field(raw, "总结") or fallback["summary"],
        "positive_notes": _parse_field(raw, "正向经验") or fallback["positive_notes"],
        "negative_notes": _parse_field(raw, "反向风险") or fallback["negative_notes"],
        "action_advice": _parse_field(raw, "下次怎么用") or fallback["action_advice"],
        "llm_provider": llm_provider,
        "llm_model": llm_model,
    }
    return parsed


def _slot_brand_id(session: Session, slot_id: int) -> int:
    slot = session.get(ScheduleSlot, slot_id)
    if slot is None:
        raise ValueError("发布样本不存在")
    return slot.brand_id


def create_experience_from_slot(session: Session, slot_id: int, experience_type: str,
                                scope: str = "campaign", platform: str = "") -> FeedbackExperience:
    sample = next((s for s in published_samples(session, _slot_brand_id(session, slot_id)) if s.slot.id == slot_id), None)
    if sample is None:
        raise ValueError("发布样本不存在")
    draft = build_experience_draft(session, slot_id, experience_type)
    draft.setdefault("llm_provider", "")
    draft.setdefault("llm_model", "")
    campaign_id = None if scope == "brand" else sample.slot.campaign_id
    entry = FeedbackExperience(
        brand_id=sample.slot.brand_id,
        campaign_id=campaign_id,
        platform=(platform.strip() or sample.slot.platform or "通用"),
        experience_type=experience_type,
        performance_level=sample.performance_level,
        source_slot_id=sample.slot.id,
        article_id=sample.slot.article_id,
        topic_id=sample.slot.topic_id,
        **draft,
    )
    session.add(entry)
    session.commit()
    session.refresh(entry)
    from app.modules.knowledge.experience_pool import sync_brand_experience_pack, sync_campaign_experience_pack
    if campaign_id is None:
        sync_brand_experience_pack(session, sample.slot.brand_id)
    else:
        sync_campaign_experience_pack(session, campaign_id)
    return entry


def create_experience_pair_from_slot(session: Session, slot_id: int, scope: str = "campaign",
                                     platform: str = "") -> list[FeedbackExperience]:
    sample = next((s for s in published_samples(session, _slot_brand_id(session, slot_id)) if s.slot.id == slot_id), None)
    if sample is None:
        raise ValueError("发布样本不存在")
    existing = session.exec(
        select(FeedbackExperience).where(
            FeedbackExperience.source_slot_id == slot_id,
            FeedbackExperience.is_active == True,
        )
    ).all()
    existing_by_type = {row.experience_type: row for row in existing}
    campaign_id = None if scope == "brand" else sample.slot.campaign_id
    entries: list[FeedbackExperience] = []
    drafts = {
        experience_type: build_experience_draft(session, slot_id, experience_type)
        for experience_type in EXPERIENCE_TYPES
        if experience_type not in existing_by_type
    }
    for draft in drafts.values():
        draft.setdefault("llm_provider", "")
        draft.setdefault("llm_model", "")
    for experience_type in EXPERIENCE_TYPES:
        if experience_type in existing_by_type:
            entries.append(existing_by_type[experience_type])
            continue
        entry = FeedbackExperience(
            brand_id=sample.slot.brand_id,
            campaign_id=campaign_id,
            platform=(platform.strip() or sample.slot.platform or "通用"),
            experience_type=experience_type,
            performance_level=sample.performance_level,
            source_slot_id=sample.slot.id,
            article_id=sample.slot.article_id,
            topic_id=sample.slot.topic_id,
            **drafts[experience_type],
        )
        session.add(entry)
        entries.append(entry)
    session.commit()
    for entry in entries:
        session.refresh(entry)
    from app.modules.knowledge.experience_pool import sync_brand_experience_pack, sync_campaign_experience_pack
    if campaign_id is None:
        sync_brand_experience_pack(session, sample.slot.brand_id)
    else:
        sync_campaign_experience_pack(session, campaign_id)
    return entries


def create_experience_pairs_from_slots(session: Session, slot_ids: list[int], scope: str = "campaign") -> list[FeedbackExperience]:
    entries: list[FeedbackExperience] = []
    for slot_id in dict.fromkeys(slot_ids):
        entries.extend(create_experience_pair_from_slot(session, slot_id, scope=scope))
    return entries


def upsert_review_rejection_experience(session: Session, article: Article, note: str) -> FeedbackExperience | None:
    """把③写作引擎的审核未通过原因沉淀为 campaign 写作经验。

    同一篇文章只保留一条活跃的审核退回经验；再次退回时更新它，避免经验包重复膨胀。
    """
    topic = session.get(Topic, article.topic_id)
    if topic is None:
        return None
    note = (note or "").strip()
    if not note:
        return None
    existing = session.exec(
        select(FeedbackExperience).where(
            FeedbackExperience.article_id == article.id,
            FeedbackExperience.experience_type == "写作经验",
            FeedbackExperience.performance_level == "审核未通过",
            FeedbackExperience.is_active == True,
        )
    ).first()
    title = f"审核未通过：{article.title or topic.title}"[:120]
    summary = (
        f"《{article.title or topic.title}》在人工审核阶段未通过。"
        f"退回原因：{note}"
    )
    positive_notes = "保留选题中具体、可感、与 campaign 相关的素材基础。"
    negative_notes = note
    action_advice = (
        "后续同 campaign 写作时，优先避开这类退回问题；生成正文前先检查标题钩子、"
        "内容深度、事实准确性、平台语气和段落结构是否满足审核意见。"
    )
    entry = existing or FeedbackExperience(
        brand_id=topic.brand_id,
        campaign_id=article.campaign_id,
        platform=article.platform or "通用",
        experience_type="写作经验",
        performance_level="审核未通过",
        source_slot_id=None,
        article_id=article.id,
        topic_id=topic.id,
        title=title,
    )
    entry.brand_id = topic.brand_id
    entry.campaign_id = article.campaign_id
    entry.platform = article.platform or "通用"
    entry.title = title
    entry.summary = summary
    entry.positive_notes = positive_notes
    entry.negative_notes = negative_notes
    entry.action_advice = action_advice
    entry.llm_provider = "manual"
    entry.llm_model = "review-rejection"
    entry.updated_at = _now()
    entry.is_active = True
    session.add(entry)
    session.commit()
    session.refresh(entry)
    from app.modules.knowledge.experience_pool import sync_brand_experience_pack, sync_campaign_experience_pack
    if entry.campaign_id is None:
        sync_brand_experience_pack(session, entry.brand_id)
    else:
        sync_campaign_experience_pack(session, entry.campaign_id)
    return entry


def experience_entries(session: Session, brand_id: int) -> list[FeedbackExperience]:
    return session.exec(
        select(FeedbackExperience)
        .where(FeedbackExperience.brand_id == brand_id, FeedbackExperience.is_active == True)
        .order_by(FeedbackExperience.updated_at.desc(), FeedbackExperience.id.desc())
    ).all()


def experience_pack_text(session: Session, brand_id: int, campaign_id: int | None,
                         experience_type: str, platform: str = "", limit: int = 8) -> str:
    platforms = {"通用", ""}
    if platform:
        platforms.add(platform)
    rows = session.exec(
        select(FeedbackExperience)
        .where(
            FeedbackExperience.brand_id == brand_id,
            FeedbackExperience.experience_type == experience_type,
            FeedbackExperience.is_active == True,
        )
        .order_by(FeedbackExperience.updated_at.desc(), FeedbackExperience.id.desc())
    ).all()
    selected: list[FeedbackExperience] = []
    for row in rows:
        if row.campaign_id not in (None, campaign_id):
            continue
        if row.platform not in platforms:
            continue
        selected.append(row)
        if len(selected) >= limit:
            break
    lines = []
    for row in selected:
        scope = "品牌常青" if row.campaign_id is None else f"campaign#{row.campaign_id}"
        lines.append(
            f"- [{row.performance_level}/{scope}/{row.platform}] {row.title}\n"
            f"  总结：{row.summary}\n"
            f"  正向经验：{row.positive_notes}\n"
            f"  反向风险：{row.negative_notes}\n"
            f"  下次怎么用：{row.action_advice}"
        )
    return "\n".join(lines)


def experience_reference_strategy_text(current_count: int = 0, inherited_count: int = 0) -> str:
    if current_count <= 0 and inherited_count > 0:
        actual = "当前 campaign 暂无经验：继承 campaign 经验 100%。"
    elif current_count < 3 and inherited_count > 0:
        actual = "当前 campaign 经验不足 3 条：当前经验约 50%，继承经验约 50%。"
    elif inherited_count > 0:
        actual = "当前 campaign 经验充足：当前经验约 70%，继承经验约 30%。"
    else:
        actual = "未关联继承经验包：当前 campaign 经验 100%。"
    return "\n".join([
        "【Campaign 总体经验包引用策略】",
        "- 当前 campaign 经验优先，用来约束本活动自己的选题和写作判断。",
        "- 继承 campaign 经验用于迁移历史打法，不能机械复刻旧标题或旧文章。",
        "- 默认权重：当前 campaign 70%，继承 campaign 30%。",
        "- 当前 campaign 经验不足时，自动提高继承 campaign 权重。",
        "- 当前 campaign 无经验时，继承 campaign 100%。",
        f"- 本次实际策略：{actual}",
    ])


def _entry_source_label(entry: FeedbackExperience) -> str:
    if entry.source_slot_id:
        return "发布复盘"
    if entry.performance_level == "审核未通过":
        return "写作审核退回"
    return "经验沉淀"


def _format_experience_entry(entry: FeedbackExperience) -> str:
    return (
        f"- [{_entry_source_label(entry)} / {entry.experience_type} / {entry.performance_level} / {entry.platform}] {entry.title}\n"
        f"  总结：{entry.summary}\n"
        f"  正向经验：{entry.positive_notes}\n"
        f"  反向风险：{entry.negative_notes}\n"
        f"  下次怎么用：{entry.action_advice}"
    )


def _task_focus_text(task: str) -> str:
    if task == "topic":
        return (
            "选题生成重点：优先吸收选题经验、历史不采纳原因、发布复盘中与切口/标题/素材选择有关的判断；"
            "避免重复低价值角度，把失败原因反向转化为更清晰的选题。"
        )
    if task == "writing":
        return (
            "写作生成重点：优先吸收写作经验、审核未通过原因、发布复盘中与标题钩子/开头/结构/语气/事实准确有关的判断；"
            "先规避退回问题，再迁移高表现做法。"
        )
    return "使用重点：按当前任务选择相关经验，保留可迁移打法，规避已验证的问题。"


def campaign_experience_context(session: Session, brand_id: int, campaign_id: int | None,
                                *, platform: str = "", task: str = "general",
                                inherited_packs: list[str] | None = None,
                                rejection_topics: list[Topic] | None = None,
                                limit: int = 10) -> str:
    """统一组装 Campaign 总体经验包，供②选题库和③写作引擎共同引用。

    底层仍保留选题/写作/发布复盘等来源；对模型统一暴露为 campaign 经验上下文。
    """
    platforms = {"通用", ""}
    if platform:
        platforms.add(platform)
    rows = session.exec(
        select(FeedbackExperience)
        .where(
            FeedbackExperience.brand_id == brand_id,
            FeedbackExperience.campaign_id == campaign_id,
            FeedbackExperience.is_active == True,
        )
        .order_by(FeedbackExperience.updated_at.desc(), FeedbackExperience.id.desc())
    ).all()
    current_entries = [row for row in rows if row.platform in platforms]
    rejection_lines = []
    for topic in rejection_topics or []:
        reason = " ".join((topic.rejection_reason or "").split())
        if reason:
            rejection_lines.append(f"- [选题回收站 / 不采纳原因] 《{topic.title}》：{reason}")
    current_count = len(current_entries) + len(rejection_lines)
    inherited = [pack.strip() for pack in (inherited_packs or []) if pack and pack.strip()]
    lines = [
        experience_reference_strategy_text(current_count=current_count, inherited_count=len(inherited)),
        "",
        "【本任务使用重点】",
        _task_focus_text(task),
        "",
        "【当前 campaign 经验（优先参考）】",
    ]
    if current_entries or rejection_lines:
        lines.extend(rejection_lines[:limit])
        remaining = max(limit - len(rejection_lines[:limit]), 0)
        lines.extend(_format_experience_entry(row) for row in current_entries[:remaining])
    else:
        lines.append("（当前 campaign 暂无沉淀经验）")
    lines.extend(["", "【继承 campaign 经验（迁移参考）】"])
    if inherited:
        for idx, pack in enumerate(inherited[:3], start=1):
            lines.append(f"--- 继承经验包 {idx} ---\n{pack[:4000]}")
    else:
        lines.append("（未关联继承经验包）")
    return "\n".join(lines).strip()


def update_experience(session: Session, entry_id: int, **fields) -> FeedbackExperience:
    entry = session.get(FeedbackExperience, entry_id)
    if entry is None:
        raise ValueError("经验不存在")
    for key, value in fields.items():
        if hasattr(entry, key):
            setattr(entry, key, value)
    entry.updated_at = _now()
    session.add(entry)
    session.commit()
    session.refresh(entry)
    from app.modules.knowledge.experience_pool import sync_brand_experience_pack, sync_campaign_experience_pack
    if entry.campaign_id is None:
        sync_brand_experience_pack(session, entry.brand_id)
    else:
        sync_campaign_experience_pack(session, entry.campaign_id)
    return entry


def deactivate_experience(session: Session, entry_id: int) -> None:
    entry = session.get(FeedbackExperience, entry_id)
    if entry is None:
        return
    entry.is_active = False
    entry.updated_at = _now()
    session.add(entry)
    session.commit()
    from app.modules.knowledge.experience_pool import sync_brand_experience_pack, sync_campaign_experience_pack
    if entry.campaign_id is None:
        sync_brand_experience_pack(session, entry.brand_id)
    else:
        sync_campaign_experience_pack(session, entry.campaign_id)
