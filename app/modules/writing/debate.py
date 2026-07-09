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


def knowledge_context_block(ctx: KnowledgeContext, writing_experience: str = "") -> str:
    """写作链路共用的知识/经验上下文块。

    ②的活动简报负责方向，资料包负责事实细节，经验包负责结构/钩子/风险规避。
    """
    pool_materials = "；".join(ctx.pool_materials) if ctx.pool_materials else "（无）"
    pool_experiences = "；".join(ctx.pool_experiences) if ctx.pool_experiences else "（无）"
    return f"""【知识库上下文】
1. 品牌约束（怎么写）
- 品牌调性：{ctx.brand_prompt or "（未设置）"}
- 内容要求：{ctx.content_notes or "（未设置）"}
- 品牌资料综合：{ctx.doc_digest or "（无）"}

2. 活动内容（写什么 / 什么时候 / 用什么素材）
- 活动简报：{ctx.campaign_digest or "（品牌常青）"}

3. 资料包（事实细节 / 可引用素材）
{pool_materials}

4. 经验包（结构 / 钩子 / 取舍 / 风险规避）
- 知识库经验：{pool_experiences}
- 发布后写作经验包：{writing_experience or "（本次未引用）"}"""


def clean_llm_output(text: str) -> str:
    """剥离模型思考段 + 代码围栏 + 常见 Markdown 标记。routes.py 也复用此函数。"""
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
    # 清理常见 Markdown 标记（LLM 习惯性输出，纯文本渲染时显示为乱码）
    import re
    # 统一换行：\r\n → \n
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    # 加粗/斜体：**text** / __text__ / *text* / _text_ → text
    t = re.sub(r'\*\*(.+?)\*\*', r'\1', t)
    t = re.sub(r'__(.+?)__', r'\1', t)
    t = re.sub(r'(?<!\w)\*(.+?)\*(?!\w)', r'\1', t)
    t = re.sub(r'(?<!\w)_(.+?)_(?!\w)', r'\1', t)
    # 行级标题：## 标题 / ### 标题 → 标题
    t = re.sub(r'(?m)^#{1,6}\s+', '', t)
    # 水平分隔线：--- / *** / ___（独占一行）→ 移除
    t = re.sub(r'(?m)^[-*_]{3,}\s*$', '', t)
    # 行内代码：`code` → code
    t = re.sub(r'`([^`]+)`', r'\1', t)
    # 引用：> text → text
    t = re.sub(r'(?m)^>\s?', '', t)
    # 无序列表标记：- / * / + 开头 → 移除标记
    t = re.sub(r'(?m)^[\s]*[-*+]\s+', '', t)
    # 有序列表标记：1. 2. → 移除数字
    t = re.sub(r'(?m)^[\s]*\d+\.\s+', '', t)
    # 链接：[text](url) → text
    t = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', t)
    # 清理多余空行（3+ 连续换行 → 2 个）
    t = re.sub(r'\n{3,}', '\n\n', t)
    return t.strip()


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
                   topic: Topic, ctx: KnowledgeContext, history: str,
                   writing_experience: str = "") -> str:
    return f"""{role_stance}

【选题】标题：{topic.title}
纲要：{topic.outline}
切入角度：{topic.angle}
受众：{topic.audience}
素材：{topic.materials}

{knowledge_context_block(ctx, writing_experience)}

【前序辩论记录】
{history}

请从你的角色立场出发，对这篇选题的切入角度、结构、素材、受众钩子提出观点（可支持、反对或补充）。
直接输出你的发言，{_DEBATE_CHARS}字以内，不要输出思考过程或分析步骤。"""


def run_debate(session: Session, article_id: int, rounds: int,
               topic: Topic, ctx: KnowledgeContext,
               writing_experience: str = "") -> str:
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
            prompt = _debate_prompt(role_key, role_name, role_stance, topic, ctx, history, writing_experience)
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
    return _synthesize_debate(all_records, topic, ctx, writing_experience)


def _synthesize_debate(records: list[DebateRecord], topic: Topic, ctx: KnowledgeContext,
                       writing_experience: str = "") -> str:
    """综合辩论记录 → 写作简报。"""
    history = "\n".join(
        f"第{r.round_num}轮 {dict((k, v) for k, v, _ in ROLES).get(r.role, r.role)}：{r.content}"
        for r in records
    )
    prompt = f"""你是写作总监。基于以下多角色辩论记录，综合出一份写作简报。

【选题】{topic.title}
{knowledge_context_block(ctx, writing_experience)}

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
    """执行 M 轮评审，每轮 4 角色评审当前图文草稿，返回综合评审摘要。"""
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
                   ctx: KnowledgeContext, style_text: str,
                   writing_experience: str = "") -> str:
    """按评审建议重写文章的 prompt。"""
    return f"""你是主笔。请基于评审综合建议，重写这篇文章。

【选题】标题：{topic.title}
纲要：{topic.outline}
受众：{topic.audience}

{knowledge_context_block(ctx, writing_experience)}

【写作风格】{style_text}

【评审综合建议】
{review_summary}

【原文章】
{article.body}

请按评审建议改进，保留优点，修正问题。
**重要**：原文章中的 `[插图：...]` 标记必须在重写后的正文中保留（格式不变，位置可调整），用于后续 AI 配图。输出格式：
标题：...

正文：...

【输出格式硬约束】
1. 纯文本输出，禁止任何 Markdown 标记：不要用 **加粗**、## 标题、--- 分隔线、`代码块`、> 引用 等。
2. 用中文标点和空行分段，不要用 Markdown 语法制造视觉层次。
3. 换行用单个 \\n，段落之间空一行；不要用 \\r\\n。
4. [插图：...] 标记只能放在完整段落之间，禁止插到句子中间或段落内部。
5. 直接输出正文，不要输出「正文：」之外的解释性文字。
"""


# ── AI 审核：动态角色 + 合规/真实性审核 ──

_AI_REVIEW_ROLE_RE = None  # 延迟编译


def _parse_ai_review_roles(text: str) -> list[tuple[str, str]]:
    """解析 LLM 输出为 [(角色名, 关注点), ...]。"""
    import re
    global _AI_REVIEW_ROLE_RE
    if _AI_REVIEW_ROLE_RE is None:
        _AI_REVIEW_ROLE_RE = re.compile(r"角色名[：:](.+?)[\n。；;]")
    lines = (text or "").splitlines()
    roles: list[tuple[str, str]] = []
    # 找所有「角色名：」行
    for i, ln in enumerate(lines):
        ln_s = ln.strip()
        if not ln_s.startswith(("角色名：", "角色名:")):
            continue
        name = ln_s.split("：", 1)[-1].split(":", 1)[-1].strip()
        if not name:
            continue
        # 往后找「关注点：」行
        focus_lines: list[str] = []
        for nxt in lines[i + 1:]:
            nxt_s = nxt.strip()
            if nxt_s.startswith(("角色名：", "角色名:")):
                break
            if nxt_s.startswith(("关注点：", "关注点:")):
                focus_lines.append(nxt_s.split("：", 1)[-1].split(":", 1)[-1].strip())
            elif focus_lines and nxt_s:
                focus_lines.append(nxt_s)
        focus = "；".join(f for f in focus_lines if f) if focus_lines else "合规性与事实准确性"
        if name:
            roles.append((name[:40], focus[:200]))
    return roles


def _generate_ai_review_roles(article: Article) -> list[tuple[str, str]]:
    """调用 LLM 根据文章内容生成 3-5 个动态审核角色。

    角色主要针对合规性、真实性等客观事实的不同方面。
    每次生成的角色不固定（temperature 由模型决定）。
    """
    body_preview = (article.body or "")[:3000]
    prompt = f"""你是审核角色生成器。请基于以下文章内容，生成 3-5 个差异化的审核角色，每个角色专注于合规性和真实性等客观事实的不同方面。

【文章标题】{article.title}
【发布平台】{article.platform or "未指定"}
【文章正文】
{body_preview}

请生成 3-5 个审核角色，可从以下方向选择（也可根据文章内容特点新增其他客观事实方向）：
- 合规审核（法律法规、广告法、平台规范）
- 事实核查（数据、引用、史料、时间线准确性）
- 版权审核（原创性、引用规范、图片来源）
- 敏感内容审核（政治敏感、社会敏感、价值观）
- 平台规范审核（符合发布平台的内容规范，如小红书/公众号）
- 逻辑一致性审核（前后矛盾、因果谬误）

严格按以下格式输出，每个角色之间用空行分隔，不要输出思考过程或其他内容：
角色名：用一个短语概括这个角色
关注点：这个角色主要审核什么（30-60字）
"""
    try:
        raw = llm.generate_text(prompt, task="ai_review_roles", module="writing", fallback=False)
    except RuntimeError:
        # 回退：内置 3 个基础审核角色
        return [
            ("合规审核员", "法律法规、广告法、平台内容规范"),
            ("事实核查员", "数据、引用、史料、时间线准确性"),
            ("版权审核员", "原创性、引用规范、图片来源"),
        ]
    roles = _parse_ai_review_roles(raw)
    if not roles:
        return [
            ("合规审核员", "法律法规、广告法、平台内容规范"),
            ("事实核查员", "数据、引用、史料、时间线准确性"),
        ]
    return roles[:5]  # 最多 5 个角色


def _ai_review_role_prompt(role_name: str, role_focus: str, article: Article) -> str:
    body_preview = (article.body or "")[:3000]
    platform_hint = ""
    if article.platform == "小红书":
        platform_hint = "本文面向小红书平台，需符合小红书内容规范（无医疗/金融违规、无虚假种草、无诱导分享）。"
    elif article.platform == "微信公众号":
        platform_hint = "本文面向微信公众号，需符合公众号内容规范（无违规推广、无不实信息、符合互联网新闻信息服务规定）。"
    return f"""你是{role_name}。{role_focus}

【文章标题】{article.title}
【发布平台】{article.platform or "未指定"}
{platform_hint}

【文章正文】
{body_preview}

请从你的角色立场审核这篇文章，针对合规性和真实性等客观事实提出具体问题。

严格按以下格式输出，不要输出思考过程或其他内容：
【审核结论】通过 / 有条件通过 / 不通过
【理由】说明你给出此结论的原因（50-150字）
【具体问题】列出发现的具体问题（如无问题写"未发现问题"）

全部回复{_DEBATE_CHARS}字以内。"""


def run_ai_review(session: Session, article_id: int, article: Article) -> str:
    """执行 AI 审核：动态生成审核角色 → 角色介绍 → 单轮审核 → 总审核员汇总。

    每次生成的角色不固定，主要针对合规性和真实性等客观事实。
    所有发言逐条落库（phase="ai_review"），用户可查看。
    - round_num=0：角色介绍（关注点），供用户了解每个角色的身份
    - round_num=1：各角色从自身领域视角分析图文，发表审核意见
    最后由总审核员 AI 结合各方意见决定是否通过并给出评价。
    LLM 失败时该角色发言记为"[审核失败]"，不中断整体。
    """
    # 1. 动态生成审核角色
    roles = _generate_ai_review_roles(article)
    # 清理该文章之前的 AI 审核记录（重新审核时）
    old = session.exec(
        select(DebateRecord).where(
            DebateRecord.article_id == article_id,
            DebateRecord.phase == "ai_review",
        )
    ).all()
    for r in old:
        session.delete(r)
    session.commit()

    # 2. 持久化角色介绍（round_num=0），让用户看到生成了哪些角色及身份
    for role_name, role_focus in roles:
        intro = DebateRecord(
            article_id=article_id, phase="ai_review",
            round_num=0, role=role_name, content=role_focus,
        )
        session.add(intro)
    session.commit()

    # 3. 单轮审核：各角色从自身领域视角分析图文，依次发言
    for role_name, role_focus in roles:
        prompt = _ai_review_role_prompt(role_name, role_focus, article)
        try:
            content = clean_llm_output(llm.generate_text(
                prompt, task="ai_review", module="writing", fallback=False))
        except RuntimeError:
            content = "[审核失败]"
        rec = DebateRecord(
            article_id=article_id, phase="ai_review",
            round_num=1, role=role_name, content=content,
        )
        session.add(rec)
        session.commit()
        session.refresh(rec)

    # 4. 总审核员 AI 综合各方意见 → 决定是否通过 + 评价
    all_records = session.exec(
        select(DebateRecord).where(
            DebateRecord.article_id == article_id, DebateRecord.phase == "ai_review"
        ).order_by(DebateRecord.round_num, DebateRecord.id)
    ).all()
    return _synthesize_ai_review(all_records, article)


def _synthesize_ai_review(records: list[DebateRecord], article: Article) -> str:
    """综合 AI 审核记录 → 审核意见。"""
    # 格式化审核历史：round_num=0 是角色介绍，round_num>=1 是审核发言
    history_lines = []
    for r in records:
        if r.round_num == 0:
            history_lines.append(f"【{r.role}】关注点：{r.content}")
        else:
            history_lines.append(f"【{r.role}】{r.content}")
    history = "\n".join(history_lines)
    prompt = f"""你是审核总监。基于以下各审核角色的意见，综合出一份审核意见，决定是否通过并给出评价。

【文章标题】{article.title}
【发布平台】{article.platform or "未指定"}
【文章正文（前2000字）】
{(article.body or "")[:2000]}

【AI 审核记录】
{history}

请综合各角色的审核结论和发现的问题，输出一份结构化的审核意见。
严格按以下结构输出，每个部分都要有内容，不要输出思考过程：

## 总体结论
[通过 / 有条件通过 / 不通过]

## 通过理由
（列出达标的方面，如果没有则写"无"）

## 不通过原因
（列出不达标、需要修改的方面，如果没有则写"无"）

## 各角色发现的问题
（按角色分组，标注严重程度：高/中/低）

## 修改建议
（针对不通过原因和问题给出具体修改建议）

## 风险评估
（合规风险、事实风险、版权风险等客观风险）"""
    try:
        return clean_llm_output(llm.generate_text(
            prompt, task="ai_review_summary", module="writing", fallback=False))
    except RuntimeError:
        return "（AI 审核综合失败，请查看各角色审核记录）"
