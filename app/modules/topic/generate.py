"""选题生成——照抄 tngen `app/topics/generate.py` 的结构（生成→按分隔符 parse→落库），
改一处：**读知识库共享契约 `KnowledgeContext`**（品牌层+活动简报+经验包三层），不直接读品牌表。

parse 用纯文本分隔符（标题：/纲要：…）而非 JSON——长中文自由文本 JSON 易碎、分隔符更鲁棒（tngen 经验）。
"""
import logging
import re

from sqlmodel import Session, select

from app.core import llm, sources
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


def _format_hits(hits: list[dict]) -> str:
    """把搜索命中压成紧凑条目喂 prompt（标题+摘要，附来源/链接）。"""
    lines = []
    for h in hits:
        title = (h.get("title") or "").strip()
        summary = (h.get("summary") or "").strip()
        src = (h.get("source") or "").strip()
        tag = f"（{src}）" if src else ""
        body = f"{title}{tag}：{summary}" if summary else f"{title}{tag}"
        lines.append("- " + body.strip("：").strip())
    return "\n".join(lines)


def _topics_prompt(kc: KnowledgeContext, existing_titles: list[str], count: int,
                   hot_hits: list[dict] | None = None) -> str:
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
    if kc.pool_experiences:   # 经验包：调打法优先级
        parts.append("【过往经验·打法参考】\n" + "\n---\n".join(kc.pool_experiences)
                     + "\n→ 优先做已验证有效的，规避曾失效的。\n")
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
                    hot_query: str = "") -> list[Topic]:
    """读知识库(KnowledgeContext) → [可选]联网搜热点 → 生成 → parse → 落 Topic。

    sources_used: 勾选的搜索源 name 列表（core/sources）；空=不联网。
    hot_query: 热点搜索关键词；空则用 品牌名(+活动名) 兜底。
    """
    kc = KnowledgeContext.load(session, brand_id, campaign_id)
    _log.info("[topic] 生成候选 brand=%s campaign=%s count=%s 勾选搜索源=%s 关键词=%r",
              brand_id, campaign_id, count, sources_used or [], hot_query or "")
    hot_hits: list[dict] = []
    if sources_used:
        query = (hot_query or "").strip() or _default_query(session, brand_id, campaign_id)
        hot_hits = sources.gather(sources_used, query)
    existing = session.exec(
        select(Topic).where(Topic.brand_id == brand_id, Topic.campaign_id == campaign_id)
        .order_by(Topic.created_at)).all()
    existing_titles = [t.title for t in existing]
    raw = llm.generate_text(_topics_prompt(kc, existing_titles, count, hot_hits),
                            task="topic_gen", module="topic")
    cands = parse_candidates(raw)[:count]
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
