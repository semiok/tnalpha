"""多角色辩论/评审/综合/重写——写作引擎的 AI 辩论链。

4 角色：主笔(writer) / 编辑(editor) / 品牌守护(brand) / 读者代表(reader)
两阶段：辩论(debate) → 综合写作简报 → 生成文章 → 评审(review) → 综合建议 → 重写

所有发言持久化到 DebateRecord 表（用户可查看每轮每角色发言）。
LLM 调用走 core.llm 统一入口（module="writing"），失败抛 RuntimeError（由调用方决定回退策略）。
"""
from sqlmodel import Session, select

from app.core import llm
from app.modules.topic.contract import KnowledgeContext
from app.modules.topic.models import Topic
from app.modules.writing.models import Article, DebateRecord

# 4 角色定义：(key, 中文名, 立场描述)
ROLES = (
    ("writer", "主笔", "你是主笔，负责推进文章的叙事策略和可读性。关注：怎么讲好故事、结构是否引人入胜、节奏感。"),
    ("editor", "编辑", "你是编辑，负责质疑和把关。关注：结构是否严谨、事实/史料是否准确、逻辑是否有漏洞。"),
    ("brand", "品牌守护", "你是品牌守护者。关注：是否偏离品牌调性、视觉一致性、母题不偏、内容要求是否满足。"),
    ("reader", "读者代表", "你是读者代表。关注：看不看得懂、开头有没有钩子、会不会想转发、受众是否对路。"),
)

_DEBATE_CHARS = 300   # 每角色每轮发言字数上限（防 prompt 膨胀）


# 思考块标签——不同模型用不同标签，用拼接避免源码中出现特殊标记
_lt, _gt, _slash = chr(0x3c), chr(0x3e), "/"
_THINK_TAGS = [
    (_lt + "think" + _gt, _lt + _slash + "think" + _gt),       # MiniMax / Claude
    (_lt + "thought" + _gt, _lt + _slash + "thought" + _gt),   # Claude 变体
]
_CONTENT_MARKERS = ["标题：", "# ", "正文：", "名称：", "作为"]


def clean_llm_output(text: str) -> str:
    """剥离模型思考段 + 代码围栏。routes.py 也复用此函数。"""
    t = (text or "").strip()
    changed = True
    while changed:
        changed = False
        for open_tag, close_tag in _THINK_TAGS:
            if open_tag in t:
                before, rest = t.split(open_tag, 1)
                if close_tag in rest:
                    _, after = rest.split(close_tag, 1)
                    t = (before + after).strip()
                else:
                    starts = [rest.find(m) for m in _CONTENT_MARKERS if rest.find(m) >= 0]
                    t = rest[min(starts):].strip() if starts else before.strip()
                changed = True
                break
    if t.startswith("```"):
        lines = t.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


def _format_debate_history(records: list[DebateRecord], phase: str, up_to_round: int) -> str:
    """把已存的发言格式化为 prompt 上下文。"""
    lines = []
    for r in records:
        if r.phase != phase or r.round_num >= up_to_round:
            continue
        name = dict((k, v) for k, v, _ in ROLES).get(r.role, r.role)
        lines.append(f"第{r.round_num}轮 {name}：{r.content}")
    return "\n".join(lines) if lines else "（首轮，尚无前序发言）"


def _debate_prompt(role_key: str, role_name: str, role_stance: str,
                   topic: Topic, ctx: KnowledgeContext, history: str) -> str:
    return f"""{role_stance}

【选题】标题：{topic.title}
纲要：{topic.outline}
切入角度：{topic.angle}
受众：{topic.audience}
素材：{topic.materials}

【品牌调性】{ctx.brand_prompt or "（未设置）"}
【活动简报】{ctx.campaign_digest or "（品牌常青）"}

【前序辩论记录】
{history}

请从你的角色立场出发，对这篇选题的切入角度、结构、素材、受众钩子提出观点（可支持、反对或补充）。
直接输出你的发言，{_DEBATE_CHARS}字以内，不要输出思考过程或分析步骤。"""


def run_debate(session: Session, article_id: int, rounds: int,
               topic: Topic, ctx: KnowledgeContext) -> str:
    """执行 N 轮辩论，每轮 4 角色依次发言，返回综合写作简报。

    所有发言逐条落库（用户可查看）。
    LLM 失败时该角色发言记为"[发言失败]"，不中断整体。
    """
    for rnd in range(1, rounds + 1):
        records = list(session.exec(
            select(DebateRecord).where(DebateRecord.article_id == article_id)
            .order_by(DebateRecord.round_num, DebateRecord.id)
        ).all()) if rnd > 1 else []
        history = _format_debate_history(records, "debate", rnd)
        for role_key, role_name, role_stance in ROLES:
            prompt = _debate_prompt(role_key, role_name, role_stance, topic, ctx, history)
            try:
                content = clean_llm_output(llm.generate_text(
                    prompt, task="debate", module="writing", fallback=False))
            except RuntimeError:
                content = "[发言失败]"
            rec = DebateRecord(
                article_id=article_id, phase="debate",
                round_num=rnd, role=role_key, content=content,
            )
            session.add(rec)
            session.commit()
            session.refresh(rec)
            history = _format_debate_history(
                list(session.exec(
                    select(DebateRecord).where(DebateRecord.article_id == article_id)
                    .order_by(DebateRecord.round_num, DebateRecord.id)
                ).all()), "debate", rnd + 1)

    # 综合所有辩论 → 写作简报
    all_records = session.exec(
        select(DebateRecord).where(
            DebateRecord.article_id == article_id, DebateRecord.phase == "debate"
        ).order_by(DebateRecord.round_num, DebateRecord.id)
    ).all()
    return _synthesize_debate(all_records, topic, ctx)


def _synthesize_debate(records: list[DebateRecord], topic: Topic, ctx: KnowledgeContext) -> str:
    """综合辩论记录 → 写作简报。"""
    history = "\n".join(
        f"第{r.round_num}轮 {dict((k, v) for k, v, _ in ROLES).get(r.role, r.role)}：{r.content}"
        for r in records
    )
    prompt = f"""你是写作总监。基于以下多角色辩论记录，综合出一份写作简报。

【选题】{topic.title}
【品牌调性】{ctx.brand_prompt or "（未设置）"}

【辩论记录】
{history}

请输出一份写作简报，包含：
1. 推荐切入角度
2. 文章结构建议（开头/中段/结尾各怎么写）
3. 必须包含的核心素材
4. 调性与文风要求
5. 配图方向

直接输出简报内容，不要输出思考过程。"""
    try:
        return clean_llm_output(llm.generate_text(
            prompt, task="debate_brief", module="writing", fallback=False))
    except RuntimeError:
        return "（辩论综合失败，使用原始选题信息生成）"


def _review_prompt(role_key: str, role_name: str, role_stance: str,
                   article: Article, history: str) -> str:
    body_preview = article.body[:2000] if article.body else "（空）"
    return f"""{role_stance}

【文章标题】{article.title}
【文章正文】
{body_preview}
【配图】{article.image_url or "（无）"}

【前序评审记录】
{history}

请从你的角色立场评审这篇文章和配图，指出具体问题（结构/事实/调性/受众/可读性）。
直接输出你的评审意见，{_DEBATE_CHARS}字以内，不要输出思考过程。"""


def run_review(session: Session, article_id: int, rounds: int, article: Article) -> str:
    """执行 M 轮评审，每轮 4 角色评审已生成图文，返回综合评审摘要。"""
    for rnd in range(1, rounds + 1):
        records = list(session.exec(
            select(DebateRecord).where(DebateRecord.article_id == article_id)
            .order_by(DebateRecord.round_num, DebateRecord.id)
        ).all()) if rnd > 1 else []
        history = _format_debate_history(records, "review", rnd)
        for role_key, role_name, role_stance in ROLES:
            prompt = _review_prompt(role_key, role_name, role_stance, article, history)
            try:
                content = clean_llm_output(llm.generate_text(
                    prompt, task="review", module="writing", fallback=False))
            except RuntimeError:
                content = "[评审失败]"
            rec = DebateRecord(
                article_id=article_id, phase="review",
                round_num=rnd, role=role_key, content=content,
            )
            session.add(rec)
            session.commit()
            session.refresh(rec)

    # 综合所有评审 → 评审摘要
    all_records = session.exec(
        select(DebateRecord).where(
            DebateRecord.article_id == article_id, DebateRecord.phase == "review"
        ).order_by(DebateRecord.round_num, DebateRecord.id)
    ).all()
    return _synthesize_review(all_records, article)


def _synthesize_review(records: list[DebateRecord], article: Article) -> str:
    history = "\n".join(
        f"第{r.round_num}轮 {dict((k, v) for k, v, _ in ROLES).get(r.role, r.role)}：{r.content}"
        for r in records
    )
    prompt = f"""你是写作总监。基于以下多角色评审记录，综合出改进建议。

【文章标题】{article.title}
【文章正文（前2000字）】
{article.body[:2000]}

【评审记录】
{history}

请输出一份综合改进建议，包含：
1. 保持的优点
2. 必须修正的问题（按优先级排序）
3. 具体修改建议（结构/调性/素材/受众/配图）

直接输出建议，不要输出思考过程。"""
    try:
        return clean_llm_output(llm.generate_text(
            prompt, task="review_summary", module="writing", fallback=False))
    except RuntimeError:
        return "（评审综合失败）"


def rewrite_prompt(article: Article, review_summary: str, topic: Topic,
                   ctx: KnowledgeContext, style_text: str) -> str:
    """按评审建议重写文章的 prompt。"""
    return f"""你是主笔。请基于评审综合建议，重写这篇文章。

【选题】标题：{topic.title}
纲要：{topic.outline}
受众：{topic.audience}

【品牌调性】{ctx.brand_prompt or "（未设置）"}
【内容要求】{ctx.content_notes or "（未设置）"}
【活动简报】{ctx.campaign_digest or "（品牌常青）"}

【写作风格】{style_text}

【评审综合建议】
{review_summary}

【原文章】
{article.body}

请按评审建议改进，保留优点，修正问题。输出格式：
标题：...

正文：...
"""
