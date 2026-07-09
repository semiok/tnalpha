"""选题生成——照抄 tngen `app/topics/generate.py` 的结构（生成→按分隔符 parse→落库），
改一处：**读知识库共享契约 `KnowledgeContext`**（品牌层+活动简报+经验包三层），不直接读品牌表。

parse 用纯文本分隔符（标题：/纲要：…）而非 JSON——长中文自由文本 JSON 易碎、分隔符更鲁棒（tngen 经验）。
"""
import logging
import re

from sqlmodel import Session, select

from app.core import llm, sources
from app.modules.feedback.experience import campaign_experience_context
from app.modules.knowledge.models import Brand, Campaign
from app.modules.topic.contract import KnowledgeContext, TopicCandidate
from app.modules.topic.models import Topic

_log = logging.getLogger("uvicorn.error")   # 进服务日志，记录每次生成的搜索选择

_LABELS = ("标题", "纲要", "受众", "时效", "素材", "配图", "时机")
# 抓某标签的值（到下一个标签或结尾）——纲要等多行也能抓全
_NEXT = "|".join(_LABELS)


def _grab(chunk: str, label: str) -> str:
    m = re.search(rf'{label}[:：]\s*(.+?)(?=\n\s*(?:{_NEXT})[:：]|$)', chunk, flags=re.S)
    return m.group(1).strip().strip("*").strip() if m else ""


def parse_candidates(text: str) -> list[TopicCandidate]:
    """从 LLM 纯文本输出按「标题：」切块，每块抽 标题/纲要/受众/时效/素材/配图/时机 → TopicCandidate。"""
    t = (text or "").strip()
    chunks = re.split(r'(?m)(?=^\s*标题[:：])', t)   # 每个选题以「标题：」开头
    out: list[TopicCandidate] = []
    for c in chunks:
        title = _grab(c, "标题")
        if not title:
            continue
        out.append(TopicCandidate(
            title=title.splitlines()[0].strip(),      # 标题只取首行
            outline=_grab(c, "纲要"), audience=_grab(c, "受众"),
            timeliness=_grab(c, "时效"), materials=_grab(c, "素材"),
            image_hint=_grab(c, "配图"), publish_window=_grab(c, "时机"),
        ))
    if not out:
        raise ValueError("未能从输出解析出选题（无 标题：/纲要： 结构）")
    return out


_HIT_SUMMARY_MAX = 240   # 每条热点摘要上限：google 会返回数百字综合长答案，截断防撑爆 prompt


def _format_hits(hits: list[dict]) -> str:
    """把搜索命中压成紧凑条目喂 prompt。**丢掉纯来源链接（无摘要）条目**（对生成无用只是噪音），
    每条摘要截断到 `_HIT_SUMMARY_MAX`（综合长答案取要点即可，避免稀释品牌/活动层）。"""
    lines = []
    for h in hits:
        summary = " ".join((h.get("summary") or "").split())   # 压平多余空白/换行
        if not summary:            # 纯来源链接、无正文 → 跳过
            continue
        if len(summary) > _HIT_SUMMARY_MAX:
            summary = summary[:_HIT_SUMMARY_MAX].rstrip() + "…"
        title = (h.get("title") or "").strip()
        src = (h.get("source") or "").strip()
        tag = f"（{src}）" if src else ""
        lines.append(f"- {title}{tag}：{summary}")
    return "\n".join(lines)


def _format_rejection_experiences(topics: list[Topic]) -> str:
    lines = []
    for t in topics:
        reason = " ".join((t.rejection_reason or "").split())
        if not reason:
            continue
        lines.append(f"- 《{t.title}》：不采纳原因：{reason}")
    return "\n".join(lines)


def _topics_prompt(kc: KnowledgeContext, existing_titles: list[str], count: int,
                   hot_hits: list[dict] | None = None,
                   campaign_experience: str = "") -> str:
    parts = [
        f"你是内容选题策划。基于以下知识库信息，生成 {count} 个**全新**的内容选题。\n",
        f"【品牌调性·约束】\n{kc.brand_prompt or '（未填）'}\n",
        f"【内容要求·约束】\n{kc.content_notes or '（未填）'}\n",
        f"【品牌内容定义（已蒸馏，直接据此）】\n{kc.doc_digest or '（暂无）'}\n",
    ]
    if hot_hits:   # 实时热点参考（联网搜索命中）：借势蹭点，但须与品牌调性/内容定义相关，别硬蹭
        parts.append(
            "【实时热点参考（联网搜索）】\n" + _format_hits(hot_hits)
            + "\n→ 可借这些热点/时事切入选题以增强时效与传播，但**必须贴合上面的品牌调性与内容定义**，"
            "不相关的热点不要硬蹭。\n")
    if kc.has_campaign:   # 活动选题：优先从简报③选题方向出发
        parts.append(
            f"【本次活动·选题简报】\n{kc.campaign_digest}\n"
            "→ 这是高时效活动选题：**优先采纳/细化简报里③选题方向**（已标受众·时效），"
            "配④关键素材，按②时效节点定发布时机。\n")
    if campaign_experience:
        parts.append(
            "【Campaign 总体经验包】\n" + campaign_experience
            + "\n→ 这是选题和写作共用的统一经验包。选题生成时优先吸收选题切口、历史不采纳原因、"
            "发布复盘中的表现判断；不要机械复刻旧标题，要迁移打法。\n")
    if kc.pool_materials:
        parts.append("【补充素材】\n" + "\n---\n".join(kc.pool_materials) + "\n")
    if existing_titles:
        parts.append("【已有选题，必须避免重复或近似】\n"
                     + "\n".join(f"- {t}" for t in existing_titles) + "\n")
    parts.append(
        f"\n为每个选题，严格按以下纯文本格式输出，不要 JSON、不要代码块、不要开场白/总结、"
        f"不要访问任何工具或数据库，直接写：\n\n"
        f"标题：一句话标题\n"
        f"纲要：100-200字，写什么、核心切入点、可用素材\n"
        f"受众：目标受众（如 城市青年/亲子）\n"
        f"时效：强 / 中 / 弱\n"
        f"素材：关联的具体素材（文物尺寸/产品/来源，无则留空）\n"
        f"配图：配图方向（无则留空）\n"
        f"时机：建议发布时机（无则留空）\n\n"
        f"每个选题之间空一行，共 {count} 个。"
        f"纲要等正文内请勿另起一行以「标题：」开头（避免被误切成新选题）。")
    return "\n".join(parts)


def _default_query(session: Session, brand_id: int, campaign_id: int | None) -> str:
    """搜索关键词兜底：用户没填时用 品牌名(+活动名) 作 query。"""
    brand = session.get(Brand, brand_id)
    parts = [brand.name] if brand else []
    if campaign_id:
        camp = session.get(Campaign, campaign_id)
        if camp:
            parts.append(camp.name)
    return " ".join(parts).strip()


def generate_topics(session: Session, brand_id: int, campaign_id: int | None = None,
                    count: int = 5, sources_used: list[str] | None = None,
                    hot_query: str = "", use_rejection_experience: bool = True,
                    use_publish_experience: bool = False) -> list[Topic]:
    """读知识库(KnowledgeContext) → [可选]联网搜热点 → 生成 → parse → 落 Topic。

    sources_used: 勾选的搜索源 name 列表（core/sources）；空=不联网。
    hot_query: 热点搜索关键词；空则用 品牌名(+活动名) 兜底。
    """
    kc = KnowledgeContext.load(session, brand_id, campaign_id)
    _log.info("[topic] 生成候选 brand=%s campaign=%s count=%s 勾选搜索源=%s 关键词=%r 参考回收站经验=%s 知识库经验包=%s",
              brand_id, campaign_id, count, sources_used or [], hot_query or "",
              use_rejection_experience, bool(kc.pool_experiences))
    hot_hits: list[dict] = []
    if sources_used:
        query = (hot_query or "").strip() or _default_query(session, brand_id, campaign_id)
        hot_hits = sources.gather(sources_used, query)
    existing = session.exec(
        select(Topic).where(Topic.brand_id == brand_id, Topic.campaign_id == campaign_id)
        .order_by(Topic.created_at)).all()
    existing_titles = [t.title for t in existing]
    rejection_experiences = []
    if use_rejection_experience:
        rejection_experiences = [t for t in existing if t.status == "回收站" and t.rejection_reason]
    campaign_experience = campaign_experience_context(
        session,
        brand_id,
        campaign_id,
        task="topic",
        inherited_packs=kc.pool_experiences,
        rejection_topics=rejection_experiences,
    )
    raw = llm.generate_text(_topics_prompt(
        kc, existing_titles, count, hot_hits, campaign_experience),
                            task="topic_gen", module="topic")
    try:
        cands = parse_candidates(raw)[:count]
    except ValueError:
        _log.error("[topic] 模型输出无法解析为选题，raw 前800字：%r", (raw or "")[:800])
        raise
    source = "added" if existing else "generated"
    created: list[Topic] = []
    for cand in cands:
        topic = Topic(brand_id=brand_id, campaign_id=campaign_id, source=source,
                      title=cand.title, outline=cand.outline, audience=cand.audience,
                      content_type=cand.content_type, timeliness=cand.timeliness,
                      materials=cand.materials, image_hint=cand.image_hint,
                      publish_window=cand.publish_window)
        session.add(topic)
        created.append(topic)
    session.commit()
    for t in created:
        session.refresh(t)
    return created


def _manual_prompt(kc: KnowledgeContext, titles: list[str], campaign_experience: str = "") -> str:
    return "\n".join([
        "你是内容选题策划。用户已经手动指定了一组选题标题，请只补全选题信息。",
        "标题必须逐字使用用户输入，不得改写、扩写、增删标点。",
        f"【品牌调性·约束】\n{kc.brand_prompt or '（未填）'}",
        f"【内容要求·约束】\n{kc.content_notes or '（未填）'}",
        f"【品牌内容定义】\n{kc.doc_digest or '（暂无）'}",
        f"【本次活动·选题简报】\n{kc.campaign_digest}" if kc.has_campaign else "【范围】品牌常青（不限活动）",
        f"【Campaign 总体经验包】\n{campaign_experience or '（无）'}",
        "【用户指定标题】\n" + "\n".join(f"{i + 1}. {title}" for i, title in enumerate(titles)),
        "",
        "请为每个标题补全字段，严格按以下格式输出；标题必须与上面完全一致：",
        "标题：用户原题",
        "纲要：100-200字，写什么、核心切入点、可用素材",
        "受众：目标受众",
        "时效：强 / 中 / 弱",
        "素材：关联的具体素材，无则留空",
        "配图：配图方向，无则留空",
        "时机：建议发布时机，无则留空",
    ])


def create_manual_topics(session: Session, brand_id: int, campaign_id: int | None,
                         titles: list[str]) -> list[Topic]:
    """手动录入标题 → 补全字段 → 落库。标题以用户输入为准，模型输出不能改标题。"""
    cleaned = []
    seen = set()
    for raw in titles:
        title = " ".join((raw or "").split())
        if not title or title in seen:
            continue
        cleaned.append(title)
        seen.add(title)
    if not cleaned:
        raise ValueError("请至少填写一个选题标题")
    kc = KnowledgeContext.load(session, brand_id, campaign_id)
    campaign_experience = campaign_experience_context(
        session,
        brand_id,
        campaign_id,
        task="topic",
        inherited_packs=kc.pool_experiences,
    )
    parsed: list[TopicCandidate] = []
    try:
        raw = llm.generate_text(_manual_prompt(kc, cleaned, campaign_experience), task="topic_manual", module="topic")
        parsed = parse_candidates(raw)
    except Exception as exc:
        _log.warning("[topic] 手动选题补全失败，按空字段落库：%s", exc)
    created: list[Topic] = []
    for idx, title in enumerate(cleaned):
        cand = parsed[idx] if idx < len(parsed) else TopicCandidate(title=title)
        topic = Topic(
            brand_id=brand_id,
            campaign_id=campaign_id,
            source="manual",
            title=title,
            outline=cand.outline,
            audience=cand.audience,
            content_type=cand.content_type,
            timeliness=cand.timeliness,
            materials=cand.materials,
            image_hint=cand.image_hint,
            publish_window=cand.publish_window,
        )
        session.add(topic)
        created.append(topic)
    session.commit()
    for topic in created:
        session.refresh(topic)
    return created
