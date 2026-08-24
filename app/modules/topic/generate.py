"""选题生成——照抄 tngen `app/topics/generate.py` 的结构（生成→按分隔符 parse→落库），
改一处：**读知识库共享契约 `KnowledgeContext`**（品牌层+活动简报+经验包三层），不直接读品牌表。

parse 用纯文本分隔符（标题：/纲要：…）而非 JSON——长中文自由文本 JSON 易碎、分隔符更鲁棒（tngen 经验）。
"""
import logging
import re
import unicodedata
from difflib import SequenceMatcher

from sqlmodel import Session, select

from app.core import llm, sources
from app.core.prompt_override import resolve
from app.modules.feedback.experience import campaign_experience_context
from app.modules.knowledge.models import Brand, Campaign
from app.modules.topic.contract import KnowledgeContext, TopicCandidate
from app.modules.topic.models import Topic

_log = logging.getLogger("uvicorn.error")   # 进服务日志，记录每次生成的搜索选择

_LABELS = ("标题", "纲要", "受众", "时效", "素材", "配图", "时机")
# 抓某标签的值（到下一个标签或结尾）——纲要等多行也能抓全
_NEXT = "|".join(_LABELS)
_BAD_OUTPUT_MARKERS = (
    "let me ",
    "i think ",
    "one more refinement",
    "final output",
    "existing topics",
    "target audience",
    "what to write",
    "core entry point",
    "usable materials",
)
_STRATEGY_EXAMPLE_TITLE_RE = re.compile(r"《([^》\n]{2,80})》")
_TITLE_SIMILARITY_THRESHOLD = 0.76
_MAX_TOPIC_GENERATION_ATTEMPTS = 3


def _grab(chunk: str, label: str) -> str:
    m = re.search(rf'{label}[:：]\s*(.+?)(?=\n\s*(?:{_NEXT})[:：]|$)', chunk, flags=re.S)
    return m.group(1).strip().strip("*").strip() if m else ""


def _looks_like_bad_candidate(cand: TopicCandidate) -> bool:
    """拦截模型把思考过程/模板占位词当作正式选题输出的污染。"""
    title = (cand.title or "").strip().lower()
    if not title or title in {"one-line title", "title", "topic title"}:
        return True
    blob = "\n".join([
        cand.title or "",
        cand.outline or "",
        cand.audience or "",
        cand.timeliness or "",
        cand.materials or "",
        cand.image_hint or "",
        cand.publish_window or "",
    ]).lower()
    return any(marker in blob for marker in _BAD_OUTPUT_MARKERS)


def parse_candidates(text: str) -> list[TopicCandidate]:
    """从 LLM 纯文本输出按「标题：」切块，每块抽 标题/纲要/受众/时效/素材/配图/时机 → TopicCandidate。"""
    t = (text or "").strip()
    chunks = re.split(r'(?m)(?=^\s*标题[:：])', t)   # 每个选题以「标题：」开头
    out: list[TopicCandidate] = []
    for c in chunks:
        title = _grab(c, "标题")
        if not title:
            continue
        cand = TopicCandidate(
            title=title.splitlines()[0].strip(),      # 标题只取首行
            outline=_grab(c, "纲要"), audience=_grab(c, "受众"),
            timeliness=_grab(c, "时效"), materials=_grab(c, "素材"),
            image_hint=_grab(c, "配图"), publish_window=_grab(c, "时机"),
        )
        if _looks_like_bad_candidate(cand):
            continue
        out.append(cand)
    if not out:
        raise ValueError("未能从输出解析出合格选题（无结构或疑似模型思考过程/模板占位）")
    return out


def extract_strategy_example_titles(contexts: list[str]) -> list[str]:
    """提取策略解释中的成品标题，作为禁用示例而不是生成答案。"""
    titles: list[str] = []
    seen: set[str] = set()
    for context in contexts:
        for raw in _STRATEGY_EXAMPLE_TITLE_RE.findall(context or ""):
            title = " ".join(raw.split()).strip()
            normalized = _normalize_title(title)
            if normalized and normalized not in seen:
                titles.append(title)
                seen.add(normalized)
    return titles


def _normalize_title(title: str) -> str:
    text = unicodedata.normalize("NFKC", title or "").lower()
    return "".join(char for char in text if char.isalnum())


def _char_bigrams(text: str) -> set[str]:
    return {text[index:index + 2] for index in range(max(0, len(text) - 1))}


def title_similarity(left: str, right: str) -> float:
    """中文短标题近似度：顺序相似度 + 字符二元组重合，取更敏感的一项。"""
    a, b = _normalize_title(left), _normalize_title(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    sequence = SequenceMatcher(None, a, b).ratio()
    a_pairs, b_pairs = _char_bigrams(a), _char_bigrams(b)
    ngram = len(a_pairs & b_pairs) / len(a_pairs | b_pairs) if a_pairs and b_pairs else 0.0
    return max(sequence, ngram)


def _duplicate_reference(title: str, exact_references: dict[str, str],
                         near_references: list[str]) -> tuple[str, float] | None:
    normalized = _normalize_title(title)
    if normalized in exact_references:
        return exact_references[normalized], 1.0
    best_title, best_score = "", 0.0
    for reference in near_references:
        score = title_similarity(title, reference)
        if score > best_score:
            best_title, best_score = reference, score
    if best_score >= _TITLE_SIMILARITY_THRESHOLD:
        return best_title, best_score
    return None


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


def _strategy_section(kc: KnowledgeContext) -> str:
    """把活动引用策略明确编号；多策略只在策略层内部等权。"""
    if not kc.strategy_contexts:
        return ""
    total = len(kc.strategy_contexts)
    items = [
        f"【策略 {index}/{total}｜策略层内部权重 1/{total}】\n{context}"
        for index, context in enumerate(kc.strategy_contexts, start=1)
    ]
    return "\n【活动明确引用的策略｜策略层内部等权】\n" + "\n---\n".join(items) + "\n"


def _topic_decision_policy(kc: KnowledgeContext, count: int) -> str:
    """把 3 层权重从软提示改成选题生成时必须执行的决策规则。"""
    strategy_count = len(kc.strategy_contexts)
    if strategy_count:
        coverage = (
            f"活动共引用 {strategy_count} 条策略，每条在策略层内部权重完全相同（各占 1/{strategy_count}）。"
            f"本次生成 {count} 个候选：数量不少于策略数时，每条策略至少成为一个候选的主要依据，"
            "其余候选尽量平均覆盖；数量少于策略数时，优先选择多条策略的真实交集，不得任意忽略其中一条。"
        )
        strategy_gate = (
            "每个候选都必须能对应至少一条引用策略中的具体人群、渠道、内容方向或执行原则；"
            "只符合泛品牌调性、却无法说明策略依据的候选，视为不合格并在输出前自行替换。"
        )
    else:
        coverage = "本活动未引用策略，策略层权重本次不生效；不得从品牌资料中自行猜测一条当前策略。"
        strategy_gate = "候选只按品牌边界与活动内容生成。"
    return (
        "\n【选题生成决策规则｜必须执行】\n"
        f"1. 权重：品牌：策略：活动 = {kc.brand_weight}:{kc.strategy_weight}:{kc.activity_weight}。"
        "权重表示选题判断的相对优先级，不是文字篇幅比例；某层无内容时跳过并按其余层重新归一。\n"
        "2. 品牌层负责不能违背的调性、受众边界、内容规范和事实背景。品牌资料综合中即使出现渠道、"
        "运营或增长打法，也只作为历史背景，不得替代活动明确引用的策略。\n"
        f"3. 策略层负责选题方向。{coverage}{strategy_gate}\n"
        "4. 活动层负责本次具体写什么、何时发布、使用哪些资料；数据池素材和经验包都计入活动层，"
        "不另设权重。热点只能帮助寻找切口，不能覆盖引用策略。\n"
        "5. 先在内部按以上规则筛选，再输出候选；不要输出分析过程。"
    )


def _topics_prompt(kc: KnowledgeContext, existing_titles: list[str], count: int,
                   hot_hits: list[dict] | None = None,
                   campaign_experience: str = "",
                   strategy_example_titles: list[str] | None = None) -> str:
    brand_prompt_text = kc.brand_prompt or "（未填）"
    content_notes_text = kc.content_notes or "（未填）"
    doc_digest_text = kc.doc_digest or "（暂无）"
    weight_section = (
        "\n【上下文参考权重】\n"
        f"品牌：策略：活动 = {kc.brand_weight}:{kc.strategy_weight}:{kc.activity_weight}\n"
        "→ 具体执行方式见下方强制决策规则。\n"
    )
    decision_policy_section = _topic_decision_policy(kc, count)
    strategy_section = _strategy_section(kc)
    hot_hits_section = ""
    if hot_hits:   # 实时热点参考（联网搜索命中）：借势蹭点，但须与品牌调性/内容定义相关，别硬蹭
        hot_hits_section = (
            "\n【实时热点参考（联网搜索）】\n" + _format_hits(hot_hits)
            + "\n→ 可借这些热点/时事切入选题以增强时效与传播，但**必须贴合上面的品牌调性与内容定义**，"
            "不相关的热点不要硬蹭。\n")
    campaign_digest_section = ""
    if kc.has_campaign:
        activity_label = "栏目" if kc.activity_type == "column" else "Campaign"
        digest = kc.campaign_digest.strip() or "（尚未解析）"
        digest_instruction = (
            "→ 优先采纳/细化简报里的③选题方向（已标受众·时效），配④关键素材，"
            "按②时效节点定发布时机。"
            if kc.campaign_digest.strip()
            else "→ 当前活动尚未解析。活动名称与类型仍是本次选题的硬范围；结合用户即时输入、引用策略"
                 "和品牌边界生成，不得虚构活动资料。"
        )
        campaign_digest_section = (
            "\n【本次活动范围｜必须遵守】\n"
            f"名称：{kc.campaign_name or '（未命名）'}\n"
            f"类型：{activity_label}\n"
            f"【活动选题简报】\n{digest}\n"
            f"{digest_instruction}\n"
        )
    campaign_experience_section = ""
    if campaign_experience:
        campaign_experience_section = (
            "\n【Campaign 总体经验包】\n" + campaign_experience
            + "\n→ 这是选题和写作共用的统一经验包。选题生成时优先吸收选题切口、历史不采纳原因、"
            "发布复盘中的表现判断；不要机械复刻旧标题，要迁移打法。\n")
    pool_materials_section = ""
    if kc.pool_materials:
        pool_materials_section = "\n【活动引用的数据池素材】\n" + "\n---\n".join(kc.pool_materials) + "\n"
    existing_titles_section = ""
    if existing_titles:
        existing_titles_section = ("\n【已有选题，必须避免重复或近似】\n"
                                   + "\n".join(f"- {t}" for t in existing_titles) + "\n")
    strategy_examples_section = ""
    if strategy_example_titles:
        strategy_examples_section = (
            "\n【策略解释中的示例标题｜禁止复用】\n"
            + "\n".join(f"- {title}" for title in strategy_example_titles)
            + "\n→ 这些标题只用于说明策略方向，不是可选答案。禁止原样使用、近义改写、换词重组，"
            "也不要沿用相同的核心命题；必须从策略原则重新推导全新选题。\n"
        )
    default = (
        "你是内容选题策划。基于以下知识库信息，生成 {count} 个**全新**的内容选题。\n"
        "\n"
        "【品牌调性·约束】\n{brand_prompt_text}\n"
        "\n"
        "【内容要求·约束】\n{content_notes_text}\n"
        "\n"
        "【品牌内容定义（已蒸馏，直接据此）】\n{doc_digest_text}\n"
        "{weight_section}"
        "{decision_policy_section}"
        "{strategy_section}"
        "{hot_hits_section}"
        "{campaign_digest_section}"
        "{campaign_experience_section}"
        "{pool_materials_section}"
        "{existing_titles_section}"
        "{strategy_examples_section}"
        "\n"
        "\n为每个选题，严格按以下纯文本格式输出，不要 JSON、不要代码块、不要开场白/总结、"
        "不要访问任何工具或数据库，直接写：\n\n"
        "标题：一句话标题\n"
        "纲要：100-200字，写什么、核心切入点、可用素材\n"
        "受众：目标受众（如 城市青年/亲子）\n"
        "时效：强 / 中 / 弱\n"
        "素材：关联的具体素材（文物尺寸/产品/来源，无则留空）\n"
        "配图：配图方向（无则留空）\n"
        "时机：建议发布时机（无则留空）\n\n"
        "每个选题之间空一行，共 {count} 个。"
        "纲要等正文内请勿另起一行以「标题：」开头（避免被误切成新选题）。"
    )
    prompt = resolve("topic:topics_prompt", default,
                     count=count,
                     brand_prompt_text=brand_prompt_text,
                     content_notes_text=content_notes_text,
                     doc_digest_text=doc_digest_text,
                     weight_section=weight_section,
                     decision_policy_section=decision_policy_section,
                     strategy_section=strategy_section,
                     hot_hits_section=hot_hits_section,
                     campaign_digest_section=campaign_digest_section,
                     campaign_experience_section=campaign_experience_section,
                     pool_materials_section=pool_materials_section,
                     existing_titles_section=existing_titles_section,
                     strategy_examples_section=strategy_examples_section)
    mandatory_prefix = ""
    if "【选题生成决策规则｜必须执行】" not in prompt:
        mandatory_prefix += weight_section + decision_policy_section + strategy_section
    if existing_titles_section and "【已有选题，必须避免重复或近似】" not in prompt:
        mandatory_prefix += existing_titles_section
    if strategy_examples_section and "【策略解释中的示例标题｜禁止复用】" not in prompt:
        mandatory_prefix += strategy_examples_section
    if campaign_digest_section and "【本次活动范围｜必须遵守】" not in prompt:
        mandatory_prefix += campaign_digest_section
    if mandatory_prefix:
        prompt = mandatory_prefix + "\n" + prompt
    return prompt


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
    brand_topics = session.exec(
        select(Topic).where(Topic.brand_id == brand_id).order_by(Topic.created_at)
    ).all()
    strategy_example_titles = extract_strategy_example_titles(kc.strategy_contexts)
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
    llm_provider, llm_model = llm.text_model_info("topic")
    exact_references = {
        _normalize_title(title): title
        for title in [*[topic.title for topic in brand_topics], *strategy_example_titles]
        if _normalize_title(title)
    }
    near_references = [*existing_titles, *strategy_example_titles]
    accepted: list[TopicCandidate] = []
    for attempt in range(1, _MAX_TOPIC_GENERATION_ATTEMPTS + 1):
        remaining = count - len(accepted)
        if remaining <= 0:
            break
        prompt_existing = [*existing_titles, *[candidate.title for candidate in accepted]]
        raw = llm.generate_text(
            _topics_prompt(
                kc, prompt_existing, remaining, hot_hits, campaign_experience,
                strategy_example_titles=strategy_example_titles,
            ),
            task="topic_gen", module="topic",
        )
        try:
            generated = parse_candidates(raw)
        except ValueError:
            _log.error("[topic] 第%s轮输出无法解析，raw 前800字：%r", attempt, (raw or "")[:800])
            if not accepted and attempt == _MAX_TOPIC_GENERATION_ATTEMPTS:
                raise
            continue
        for candidate in generated:
            duplicate = _duplicate_reference(candidate.title, exact_references, near_references)
            if duplicate:
                reference, score = duplicate
                _log.info(
                    "[topic] 拒绝重复候选 title=%r reference=%r similarity=%.2f attempt=%s",
                    candidate.title, reference, score, attempt,
                )
                continue
            accepted.append(candidate)
            exact_references[_normalize_title(candidate.title)] = candidate.title
            near_references.append(candidate.title)
            if len(accepted) >= count:
                break
    cands = accepted[:count]
    if not cands:
        raise ValueError("生成结果均与历史选题或策略示例重复，请补充活动信息或调整策略后重试")
    if len(cands) < count:
        _log.warning("[topic] 去重补生成后仍不足：请求=%s 实际=%s", count, len(cands))
    source = "added" if existing else "generated"
    created: list[Topic] = []
    for cand in cands:
        topic = Topic(brand_id=brand_id, campaign_id=campaign_id, source=source,
                      title=cand.title, outline=cand.outline, audience=cand.audience,
                      content_type=cand.content_type, timeliness=cand.timeliness,
                      materials=cand.materials, image_hint=cand.image_hint,
                      publish_window=cand.publish_window,
                      llm_provider=llm_provider, llm_model=llm_model)
        session.add(topic)
        created.append(topic)
    session.commit()
    for t in created:
        session.refresh(t)
    return created


def _instant_prompt(kc: KnowledgeContext, brief: str, existing_titles: list[str],
                    strategy_example_titles: list[str], campaign_experience: str = "") -> str:
    """即时输入既可以是想法/要求，也可以是粘贴的外部案例或素材。"""
    instant_section = (
        "【用户即时输入｜本次任务核心】\n"
        f"{brief}\n\n"
        "识别并执行规则：\n"
        "1. 输入可能混合灵感、外部品牌案例、素材原文和具体要求，先判断用户真正希望生成什么。\n"
        "2. 明确要求优先执行；除非用户明确要求保留，外部案例只借鉴选题方法和信息结构，"
        "不复制其品牌名称、专属事实、标题或原文表达。\n"
        "3. 用户输入属于活动层的本次核心素材，但不能突破品牌边界或活动引用策略。\n"
        "4. 不把未核实内容扩写成事实；资料不足时收窄表达，不要补造。\n"
        "5. 最终只输出 1 个可执行的全新候选选题，不输出分析过程。"
    )
    base_prompt = _topics_prompt(
        kc,
        existing_titles,
        1,
        campaign_experience=campaign_experience,
        strategy_example_titles=strategy_example_titles,
    )
    default = "{instant_section}\n\n{base_prompt}"
    prompt = resolve(
        "topic:instant_prompt",
        default,
        instant_section=instant_section,
        base_prompt=base_prompt,
        brief=brief,
    )
    mandatory_prefix = ""
    if "【用户即时输入｜本次任务核心】" not in prompt:
        mandatory_prefix += instant_section + "\n\n"
    if "【选题生成决策规则｜必须执行】" not in prompt:
        mandatory_prefix += base_prompt + "\n\n"
    elif kc.has_campaign and "【本次活动范围｜必须遵守】" not in prompt:
        activity_label = "栏目" if kc.activity_type == "column" else "Campaign"
        mandatory_prefix += (
            "【本次活动范围｜必须遵守】\n"
            f"名称：{kc.campaign_name or '（未命名）'}\n"
            f"类型：{activity_label}\n\n"
        )
    return mandatory_prefix + prompt


def create_topic_from_brief(session: Session, brand_id: int, campaign_id: int | None,
                            brief: str) -> Topic:
    """即时想法/案例/指令 → AI 识别 → 单个去重候选选题。"""
    cleaned = (brief or "").strip()
    if not cleaned:
        raise ValueError("请输入灵感、参考内容或具体要求")
    if len(cleaned) > 20_000:
        raise ValueError("输入内容不能超过 20000 字")

    kc = KnowledgeContext.load(session, brand_id, campaign_id)
    existing = session.exec(
        select(Topic).where(Topic.brand_id == brand_id, Topic.campaign_id == campaign_id)
        .order_by(Topic.created_at)
    ).all()
    brand_topics = session.exec(
        select(Topic).where(Topic.brand_id == brand_id).order_by(Topic.created_at)
    ).all()
    existing_titles = [topic.title for topic in existing]
    strategy_example_titles = extract_strategy_example_titles(kc.strategy_contexts)
    rejection_topics = [
        topic for topic in existing if topic.status == "回收站" and topic.rejection_reason
    ]
    campaign_experience = campaign_experience_context(
        session,
        brand_id,
        campaign_id,
        task="topic",
        inherited_packs=kc.pool_experiences,
        rejection_topics=rejection_topics,
    )
    exact_references = {
        _normalize_title(title): title
        for title in [*[topic.title for topic in brand_topics], *strategy_example_titles]
        if _normalize_title(title)
    }
    near_references = [*existing_titles, *strategy_example_titles]
    rejected_titles: list[str] = []
    candidate: TopicCandidate | None = None
    for attempt in range(1, _MAX_TOPIC_GENERATION_ATTEMPTS + 1):
        raw = llm.generate_text(
            _instant_prompt(
                kc,
                cleaned,
                [*existing_titles, *rejected_titles],
                strategy_example_titles,
                campaign_experience,
            ),
            task="topic_instant",
            module="topic",
        )
        try:
            generated = parse_candidates(raw)
        except ValueError:
            _log.error("[topic] 即时选题第%s轮无法解析，raw 前800字：%r", attempt, (raw or "")[:800])
            if attempt == _MAX_TOPIC_GENERATION_ATTEMPTS:
                raise
            continue
        for item in generated:
            duplicate = _duplicate_reference(item.title, exact_references, near_references)
            if duplicate:
                reference, score = duplicate
                rejected_titles.append(item.title)
                _log.info(
                    "[topic] 拒绝重复即时选题 title=%r reference=%r similarity=%.2f attempt=%s",
                    item.title, reference, score, attempt,
                )
                continue
            candidate = item
            break
        if candidate is not None:
            break
    if candidate is None:
        raise ValueError("生成结果与历史选题或策略示例重复，请补充更具体的想法后重试")

    llm_provider, llm_model = llm.text_model_info("topic")
    topic = Topic(
        brand_id=brand_id,
        campaign_id=campaign_id,
        source="instant",
        title=candidate.title,
        outline=candidate.outline,
        audience=candidate.audience,
        content_type=candidate.content_type,
        timeliness=candidate.timeliness,
        materials=candidate.materials,
        image_hint=candidate.image_hint,
        publish_window=candidate.publish_window,
        llm_provider=llm_provider,
        llm_model=llm_model,
    )
    session.add(topic)
    session.commit()
    session.refresh(topic)
    return topic
