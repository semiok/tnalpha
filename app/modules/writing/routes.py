"""③写作引擎：采纳选题 → 风格注入 → 文章 → 配图。

边界：读② Topic(status='采纳')，写③ Article/Style；不回写 Topic.status。
"""
import json
import threading
import os
import re
import shutil
import time
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select
from starlette.requests import Request

from app.core import auth, config, db, docparse, llm, storage
from app.core.db import get_session
from app.core.prompt_override import resolve
from app.core.templates import create_templates
from app.modules.feedback.experience import campaign_experience_context, upsert_review_rejection_experience
from app.modules.knowledge.models import Brand, Campaign
from app.modules.topic.contract import KnowledgeContext
from app.modules.topic.models import Topic
from app.modules.writing.debate import clean_llm_output, knowledge_context_block, rewrite_prompt, run_ai_review, run_debate, run_review
from app.modules.writing.models import (
    ARTICLE_STATUSES,
    PLATFORMS,
    STYLE_SOURCES,
    Article,
    ArticleImage,
    DebateRecord,
    Style,
    StyleDiscussion,
    StyleDiscussionMessage,
    StyleDoc,
    StyleRevision,
    WritingReq,
    _now,
)

router = APIRouter()
templates = create_templates()


def _strip_markdown(text: str) -> str:
    """Jinja2 过滤器：剥离 Markdown 标记，防止纯文本渲染时显示为乱码。"""
    import re as _re
    if not text:
        return ""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = _re.sub(r'\*\*(.+?)\*\*', r'\1', t)
    t = _re.sub(r'(?<!\w)\*(.+?)\*(?!\w)', r'\1', t)
    t = _re.sub(r'(?m)^#{1,6}\s+', '', t)
    t = _re.sub(r'(?m)^[-*_]{3,}\s*$', '', t)
    t = _re.sub(r'`([^`]+)`', r'\1', t)
    t = _re.sub(r'(?m)^>\s?', '', t)
    t = _re.sub(r'(?m)^[\s]*[-*+]\s+', '', t)
    t = _re.sub(r'(?m)^[\s]*\d+\.\s+', '', t)
    t = _re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', t)
    t = _re.sub(r'\n{3,}', '\n\n', t)
    return t.strip()


templates.env.filters["strip_markdown"] = _strip_markdown


def _jinja_combine(base: dict, extra: dict) -> dict:
    """Jinja2 过滤器：合并两个 dict（后者覆盖前者），用于模板内动态构建角色→颜色映射。"""
    out = dict(base or {})
    out.update(extra or {})
    return out


templates.env.filters["combine"] = _jinja_combine
RUNNING_ARTICLE_STATUSES = ("辩论中", "写作中", "重写中", "待配图", "AI审核中")
# AI 审核综合失败时的兜底文案（见 debate._synthesize_ai_review），出现它代表"未有效审过"
AI_REVIEW_FAIL_MARKER = "（AI 审核综合失败"


def _ai_review_effective(summary: str | None) -> bool:
    """判断文章是否已有一份有效的 AI 审核意见（可据此拒绝重复审核）。

    空或失败兜底文案 → 未有效审过，允许重试；其余 → 已审过。
    """
    if not summary:
        return False
    return not summary.startswith(AI_REVIEW_FAIL_MARKER)


def _has_ai_review_records(session: Session, article_id: int) -> bool:
    """是否存有 ai_review 阶段的 DebateRecord（角色介绍/发言）。"""
    return session.exec(
        select(DebateRecord.id).where(
            DebateRecord.article_id == article_id,
            DebateRecord.phase == "ai_review",
        )
    ).first() is not None


def _clear_ai_review_records(session: Session, article_id: int) -> None:
    """删除某文章全部 ai_review 阶段的 DebateRecord（编辑正文后旧意见失效）。"""
    for r in session.exec(
        select(DebateRecord).where(
            DebateRecord.article_id == article_id,
            DebateRecord.phase == "ai_review",
        )
    ).all():
        session.delete(r)

# 配图子线程互斥锁：防止同一文章并发跑多个 worker（用户双击「补生」等场景）
_image_worker_locks: dict[int, threading.Lock] = {}
_image_worker_locks_guard = threading.Lock()


def _get_article_lock(article_id: int) -> threading.Lock:
    with _image_worker_locks_guard:
        lock = _image_worker_locks.get(article_id)
        if lock is None:
            lock = threading.Lock()
            _image_worker_locks[article_id] = lock
        return lock


def recover_zombie_articles() -> int:
    """启动时恢复僵尸文章：worker 线程随进程死亡后，状态仍卡在「生成中/AI审核中」。

    策略：
    - AI审核中 → 待审核（文章已生成完毕，仅审核被中断，用户可重新发起 AI 审核或人工审核）
    - 待配图 / 辩论中 / 写作中 / 重写中 → 保留状态但补 error_message（无完整正文，提示用户删除重生成）

    返回恢复的文章数。在 main.py lifespan 的 init_db() 后调用。
    """
    session = next(get_session())
    try:
        zombies = session.exec(
            select(Article).where(Article.status.in_(RUNNING_ARTICLE_STATUSES))
        ).all()
        recovered = 0
        for a in zombies:
            if a.status == "AI审核中":
                a.status = "待审核"
                a.error_message = "AI 审核因服务器重启中断，已自动恢复为待审核。"
                a.updated_at = _now()
                session.add(a)
                recovered += 1
            elif not a.error_message:
                a.error_message = "生成因服务器重启中断，请删除后重新生成。"
                a.updated_at = _now()
                session.add(a)
                recovered += 1
        if recovered:
            session.commit()
        return recovered
    finally:
        session.close()


def _first_brand(session: Session) -> Brand | None:
    return session.exec(select(Brand).order_by(Brand.id)).first()


def _default_style(session: Session, brand_id: int) -> Style | None:
    style = session.exec(
        select(Style).where(
            Style.brand_id == brand_id,
            Style.is_default == True,
            Style.source != "preset",
        ).order_by(Style.id)
    ).first()
    if style is not None:
        return style
    return session.exec(select(Style).where(
        Style.brand_id == brand_id,
        Style.source != "preset",
    ).order_by(Style.id)).first()


def _active_article_query():
    return select(Article).where(Article.deleted_at == None, Article.status != "已删除")


def _article_title(text: str, fallback: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("标题："):
            return line.split("：", 1)[1].strip() or fallback
        if line.startswith("# "):
            return line[2:].strip() or fallback
    return fallback


def _parse_styles(text: str) -> list[tuple[str, str]]:
    """解析 LLM 输出为 [(name, summary), ...]。

    容错：某些模型（如 minimax-m3）会在正式输出前带一段思考过程。
    策略：从后往前找「名称：」行，确保取到最终答案而非思考中的草稿。
    """
    lines = (text or "").splitlines()
    # 找所有「名称：」行的位置（倒序，跳过思考段里的草稿）
    name_indices = [i for i, ln in enumerate(lines) if ln.strip().startswith(("名称：", "名称:"))]
    out: list[tuple[str, str]] = []
    for ni in name_indices:
        name = lines[ni].strip().split("：", 1)[-1].split(":", 1)[-1].strip()
        if not name:
            continue
        # 从名称行往后找「总结：」行
        summary_lines: list[str] = []
        for ln in lines[ni + 1:]:
            ln = ln.strip()
            if ln.startswith(("名称：", "名称:")):
                break  # 下一个风格开始
            if ln.startswith(("总结：", "总结:")):
                summary_lines.append(ln.split("：", 1)[-1].split(":", 1)[-1].strip())
            elif summary_lines and ln:
                summary_lines.append(ln)
        if name and summary_lines:
            out.append((name[:80], "\n".join(summary_lines)))
    return out


def _style_discussion_source_context(session: Session, style: Style) -> tuple[list[StyleDoc], str]:
    """收集风格讨论需要的上传资料上下文，限制总长度避免撑爆 prompt。"""
    docs = session.exec(
        select(StyleDoc).where(StyleDoc.style_id == style.id)
        .order_by(StyleDoc.created_at, StyleDoc.id)
    ).all()
    parts = []
    for doc in docs:
        text = (doc.extracted_text or "").strip()
        note = (doc.note or "").strip()
        block = [f"文件：{doc.filename}"]
        if note:
            block.append(f"用户说明：{note}")
        block.append(f"解析文本：{text[:6000] if text else '（未提取到正文）'}")
        parts.append("\n".join(block))
    source_context = "\n\n".join(parts)[:18000]
    if not source_context:
        source_context = "（当前风格没有关联的上传文档；请以风格总结和参考链接为准。）"
    return docs, source_context


def _style_discussion_history(messages: list[StyleDiscussionMessage], limit: int = 12) -> str:
    """保留最近对话并限制总字数，避免长回复让后续请求越来越慢。"""
    labels = {"user": "用户", "assistant": "AI"}
    recent = messages[-limit:]
    if not recent:
        return "（还没有历史对话）"
    max_total_chars = 24000
    max_message_chars = 5000
    selected: list[str] = []
    used = 0
    for message in reversed(recent):
        content = (message.content or "").strip()
        if len(content) > max_message_chars:
            content = (
                content[:max_message_chars // 2]
                + "\n……（本条较长，已截取中间内容）……\n"
                + content[-max_message_chars // 2:]
            )
        item = f"{labels.get(message.role, message.role)}：{content}"
        separator_chars = 2 if selected else 0
        remaining = max_total_chars - used - separator_chars
        if remaining <= 0:
            break
        if len(item) > remaining:
            marker = "\n……（历史上下文已截断）"
            item = item[:max(0, remaining - len(marker))] + marker[:remaining]
        selected.append(item)
        used += len(item) + separator_chars
    return "\n\n".join(reversed(selected))


def _style_discussion_prompt(style: Style, ctx: KnowledgeContext,
                             source_context: str, history: str,
                             user_message: str, draft_name: str = "",
                             draft_summary: str = "") -> str:
    """风格讨论 prompt：回答问题，并在合适时给出结构化修改草案。"""
    default = """你是品牌写作风格顾问，正在和用户一起打磨一套可长期复用的品牌写作风格。

你的任务是先理解用户想解决的问题，再给出具体、可执行的建议。不要擅自修改数据库；只有用户点击“应用到当前风格”后，修改才会生效。

【品牌约束】
- 品牌调性：{brand_prompt}
- 内容要求：{content_notes}
- 品牌资料综合：{doc_digest}

【当前写作风格】
- 名称：{style.name}
- 总结：{style.summary}
- 来源：{style.source}
- 参考链接：{style.reference_url}

【之前上传文件和解析内容】
{source_context}

【已有修改草案】
- 名称：{draft_name}
- 总结：{draft_summary}

【最近对话】
{history}

【用户最新消息】
{user_message}

请用中文回答，重点讨论“为什么这样改、会带来什么取舍、如何避免失去原风格的核心特征”。
如果用户只是提问或还没有明确要改什么，正常回答，不要强行生成修改稿。
如果已经形成了明确的修改方案，请在回答末尾严格按以下格式输出完整修改草案：

【修改后名称】
新的风格名称

【修改后总结】
新的完整风格总结，必须可以直接注入文章生成 prompt

【修改理由】
说明改了什么、保留了什么、有什么取舍
"""
    return resolve(
        "writing:style_discussion_prompt",
        default,
        style=style,
        brand_prompt=ctx.brand_prompt or "（未设置）",
        content_notes=ctx.content_notes or "（未设置）",
        doc_digest=ctx.doc_digest or "（无）",
        source_context=source_context,
        history=history,
        user_message=user_message,
        draft_name=draft_name or "（暂无）",
        draft_summary=draft_summary or "（暂无）",
    )


def _extract_style_discussion_section(text: str, marker: str, next_markers: tuple[str, ...]) -> str:
    lines = (text or "").splitlines()
    for index, line in enumerate(lines):
        if marker not in line:
            continue
        first = line.split(marker, 1)[1].lstrip("：: ").strip()
        values = [first] if first else []
        for following in lines[index + 1:]:
            if any(stop in following for stop in next_markers):
                break
            if following.strip():
                values.append(following.strip())
        return "\n".join(values).strip()
    return ""


def _parse_style_discussion_draft(text: str) -> tuple[str, str, str] | None:
    """解析 AI 回复中的修改草案；普通问答没有完整三段时返回 None。"""
    clean = clean_llm_output(text)
    name = _extract_style_discussion_section(
        clean, "【修改后名称】", ("【修改后总结】", "【修改理由】")
    )
    summary = _extract_style_discussion_section(
        clean, "【修改后总结】", ("【修改后名称】", "【修改理由】")
    )
    reason = _extract_style_discussion_section(
        clean, "【修改理由】", ("【修改后名称】", "【修改后总结】")
    )
    if name and summary:
        return name[:120], summary[:12000], reason[:4000]
    return None


def _get_style_for_discussion(style_id: int, request: Request, session: Session) -> Style:
    auth.require_level(request, 1)
    style = session.get(Style, style_id)
    brand = _first_brand(session)
    if style is None or brand is None or style.brand_id != brand.id or style.source == "preset":
        raise HTTPException(404, "风格不存在")
    return style


def _get_or_create_style_discussion(session: Session, style: Style) -> StyleDiscussion:
    discussion = session.exec(
        select(StyleDiscussion).where(StyleDiscussion.style_id == style.id)
    ).first()
    if discussion is not None:
        return discussion
    discussion = StyleDiscussion(style_id=style.id, brand_id=style.brand_id)
    session.add(discussion)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        discussion = session.exec(
            select(StyleDiscussion).where(StyleDiscussion.style_id == style.id)
        ).first()
        if discussion is None:
            raise
    else:
        session.refresh(discussion)
    return discussion


def _extract_style_prompt(url: str, text: str) -> str:
    """URL 提取 prompt：从网页正文提炼一个可复用的写作风格。"""
    text_body = text[:6000]
    default = """请分析以下网页内容的写作风格，提炼出一个可复用的写作风格总结。

【来源URL】
{url}

【网页正文】
{text_body}

直接按以下格式输出，不要输出思考过程、分析步骤或其他任何内容：
名称：用一个短语概括这种风格
总结：对该风格的写作特征进行全面描述
"""
    return resolve("writing:extract_style_prompt", default,
                   url=url, text_body=text_body)

def _manual_style_prompt(filenames: list[str], note: str, text: str) -> str:
    """手动上传 prompt：从用户上传的文档正文（可带文字说明）提炼一个可复用的写作风格。

    filenames=上传文件名列表（用于让模型知道来源形态）；note=用户写的文字说明（可为空）。
    """
    src_label = "、".join(filenames) if filenames else "（无文件，仅文字说明）"
    note_block = f"【用户说明】\n{note.strip()}\n" if note.strip() else ""
    text_body = text[:6000]
    default = """请分析以下内容的写作风格，提炼出一个可复用的写作风格总结。

【来源文件】
{src_label}

{note_block}【文档正文】
{text_body}

直接按以下格式输出，不要输出思考过程、分析步骤或其他任何内容：
名称：用一个短语概括这种风格
总结：对该风格的写作特征进行全面描述
"""
    return resolve("writing:manual_style_prompt", default,
                   src_label=src_label, note_block=note_block, text_body=text_body)


def _fetch_url_text(url: str, timeout: int = 20) -> str:
    """抓 URL 页面正文（urllib + bs4，去脚本/样式/导航，截断喂 LLM）。"""
    import urllib.request
    from bs4 import BeautifulSoup

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; tnalpha/1.0)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        html = r.read().decode(r.headers.get_content_charset() or "utf-8", "replace")
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines)[:8000]


@router.get("/writing")
def writing_home(request: Request, session: Session = Depends(get_session)):
    status_filter = request.query_params.get("status", "全部")
    # 筛选栏分组标签：映射到实际 status 值
    STATUS_GROUPS = {
        "生成中": ("辩论中", "写作中", "重写中", "待配图", "AI审核中"),
        "待审核": ("待审核",),
        "审核通过": ("已审核",),
        "审核未通过": ("审核未通过",),
        "已删除": ("已删除",),
    }
    if status_filter not in (*STATUS_GROUPS, "全部"):
        status_filter = "全部"
    tab = request.query_params.get("tab", "library")
    if tab not in ("library", "new"):
        tab = "library"
    highlight_raw = request.query_params.get("highlight")
    try:
        highlight = int(highlight_raw) if highlight_raw else None
    except ValueError:
        highlight = None
    style_sync = request.query_params.get("style_sync")
    if style_sync not in ("applied", "created"):
        style_sync = None
    style_sync_id = request.query_params.get("style_sync_id")
    brand = _first_brand(session)
    topics: list[Topic] = []
    campaigns: list[Campaign] = []
    article_rows: list[dict] = []
    topic_articles: dict[int, list[Article]] = {}
    styles: list[Style] = []
    has_default_style = False
    if brand is not None:
        campaigns = session.exec(
            select(Campaign).where(Campaign.brand_id == brand.id).order_by(Campaign.id)
        ).all()
        # 展示选题库中所有已采纳选题，作为写作引擎的待写作输入
        topics = session.exec(
            select(Topic).where(Topic.brand_id == brand.id, Topic.status == "采纳").order_by(Topic.created_at.desc())
        ).all()
        topic_ids = [t.id for t in topics if t.id is not None]
        if topic_ids:
            for article in session.exec(
                _active_article_query()
                .where(Article.topic_id.in_(topic_ids))
                .order_by(Article.updated_at.desc(), Article.id.desc())
            ).all():
                topic_articles.setdefault(article.topic_id, []).append(article)
        article_q = select(Article).order_by(Article.updated_at.desc(), Article.id.desc())
        if status_filter == "全部":
            # 全部 = 含已删除在内的所有状态
            pass
        else:
            group_statuses = STATUS_GROUPS[status_filter]
            if status_filter == "已删除":
                article_q = article_q.where(Article.status.in_(group_statuses))
            else:
                article_q = article_q.where(Article.deleted_at == None, Article.status != "已删除")
                article_q = article_q.where(Article.status.in_(group_statuses))
        all_topic_ids = [a.topic_id for a in session.exec(article_q).all()]
        topic_map = {t.id: t for t in session.exec(select(Topic).where(Topic.id.in_(all_topic_ids))).all()} if all_topic_ids else {}
        for article in session.exec(article_q).all():
            topic = topic_map.get(article.topic_id)
            if topic is None or topic.brand_id != brand.id:
                continue
            records = []
            if article.status in RUNNING_ARTICLE_STATUSES:
                records = session.exec(
                    select(DebateRecord).where(DebateRecord.article_id == article.id)
                    .order_by(DebateRecord.round_num, DebateRecord.id)
                ).all()
            article_rows.append({"article": article, "topic": topic, "records": records})
        all_styles = session.exec(
            select(Style)
            .where(Style.brand_id == brand.id, Style.source != "preset")
            .order_by(Style.is_default.desc(), Style.id)
        ).all()
        styles = all_styles
        has_default_style = any(st.is_default for st in all_styles)
        default_style = next((st for st in all_styles if st.is_default), None)
        # 手动上传历史文档（按上传时间倒序，供「手动上传」tab 展示）
        style_docs = session.exec(
            select(StyleDoc).where(StyleDoc.brand_id == brand.id)
            .order_by(StyleDoc.created_at.desc(), StyleDoc.id.desc())
        ).all()
        style_map = {st.id: st for st in all_styles}
        # 手动上传风格的来源文件名映射：style_id → [filename, ...]
        style_source_files: dict[int, list[str]] = {}
        if style_docs:
            for doc in style_docs:
                if doc.style_id:
                    style_source_files.setdefault(doc.style_id, []).append(doc.filename)
        # 已保存的写作要求列表（供生成弹窗下拉选择）
        writing_reqs = session.exec(
            select(WritingReq).where(WritingReq.brand_id == brand.id)
            .order_by(WritingReq.created_at.desc(), WritingReq.id.desc())
        ).all()
    else:
        style_docs = []
        style_map = {}
        style_source_files = {}
        default_style = None
        writing_reqs = []
    cmap = {c.id: c.name for c in campaigns}
    return templates.TemplateResponse(request, "writing/home.html", {
        "brand": brand,
        "topics": topics,
        "campaigns": campaigns,
        "cmap": cmap,
        "article_rows": article_rows,
        "topic_articles": topic_articles,
        "status_filter": status_filter,
        "article_statuses": ARTICLE_STATUSES,
        "status_filters": ("全部", "生成中", "待审核", "审核通过", "审核未通过", "已删除"),
        "styles": styles,
        "has_default_style": has_default_style,
        "default_style": default_style,
        "style_sources": STYLE_SOURCES,
        "tab": tab,
        "highlight": highlight,
        "style_sync": style_sync,
        "style_sync_id": style_sync_id,
        "level": getattr(request.state, "level", 0),
        "platforms": PLATFORMS,
        "style_docs": style_docs,
        "style_map": style_map,
        "style_source_files": style_source_files,
        "writing_reqs": writing_reqs,
    })


@router.post("/writing/styles/{style_id}/default")
def set_default_style(style_id: int, request: Request, session: Session = Depends(get_session)):
    auth.require_level(request, 1)
    style = session.get(Style, style_id)
    if style is None:
        raise HTTPException(404, "风格不存在")
    peers = session.exec(select(Style).where(Style.brand_id == style.brand_id)).all()
    for peer in peers:
        peer.is_default = peer.id == style_id
        session.add(peer)
    session.commit()
    return RedirectResponse("/writing", status_code=303)


@router.post("/writing/styles/unset-default")
def unset_default_style(request: Request, session: Session = Depends(get_session)):
    """取消品牌默认风格，让 AI 按品牌要求自行决定文风。"""
    auth.require_level(request, 1)
    brand = _first_brand(session)
    if brand is None or brand.id is None:
        raise HTTPException(404, "品牌不存在")
    peers = session.exec(select(Style).where(Style.brand_id == brand.id)).all()
    for peer in peers:
        peer.is_default = False
        session.add(peer)
    session.commit()
    return RedirectResponse("/writing", status_code=303)


@router.post("/writing/styles/{style_id}/delete")
def delete_style(style_id: int, request: Request, session: Session = Depends(get_session)):
    auth.require_level(request, 1)
    style = session.get(Style, style_id)
    if style is None:
        raise HTTPException(404, "风格不存在")
    session.delete(style)
    session.commit()
    return RedirectResponse("/writing", status_code=303)


def _style_discussion_page_context(request: Request, session: Session,
                                   style: Style, discussion: StyleDiscussion) -> dict:
    docs, source_context = _style_discussion_source_context(session, style)
    messages = session.exec(
        select(StyleDiscussionMessage)
        .where(StyleDiscussionMessage.discussion_id == discussion.id)
        .order_by(StyleDiscussionMessage.created_at, StyleDiscussionMessage.id)
    ).all()
    return {
        "request": request,
        "style": style,
        "discussion": discussion,
        "messages": messages,
        "source_docs": docs,
        "source_context": source_context,
        "style_sources": STYLE_SOURCES,
    }


@router.get("/writing/styles/{style_id}/discussion")
def style_discussion(style_id: int, request: Request,
                     session: Session = Depends(get_session)):
    """打开某个写作风格的持久化 AI 讨论会话。"""
    style = _get_style_for_discussion(style_id, request, session)
    discussion = _get_or_create_style_discussion(session, style)
    return templates.TemplateResponse(
        request,
        "writing/_style_discussion.html",
        _style_discussion_page_context(request, session, style, discussion),
    )


@router.get("/writing/styles/{style_id}/discussion/page")
def style_discussion_page(style_id: int, request: Request,
                          session: Session = Depends(get_session)):
    """以独立页面打开某个写作风格的持久化 AI 讨论会话。"""
    style = _get_style_for_discussion(style_id, request, session)
    discussion = _get_or_create_style_discussion(session, style)
    context = _style_discussion_page_context(request, session, style, discussion)
    context["full_page"] = True
    return templates.TemplateResponse(request, "writing/style_discussion.html", context)


@router.post("/writing/styles/{style_id}/discussion")
def style_discussion_message(style_id: int, request: Request,
                             message: str = Form(""),
                             session: Session = Depends(get_session)):
    """发送一条风格讨论消息，并持久化 AI 回复与修改草案。"""
    style = _get_style_for_discussion(style_id, request, session)
    message = (message or "").strip()
    if not message:
        raise HTTPException(400, "请输入想讨论的修改方向")
    if len(message) > 4000:
        raise HTTPException(400, "单条消息不能超过 4000 字")

    discussion = _get_or_create_style_discussion(session, style)
    user_message = StyleDiscussionMessage(
        discussion_id=discussion.id,
        role="user",
        content=message,
    )
    session.add(user_message)
    session.commit()

    messages = session.exec(
        select(StyleDiscussionMessage)
        .where(StyleDiscussionMessage.discussion_id == discussion.id)
        .order_by(StyleDiscussionMessage.created_at, StyleDiscussionMessage.id)
    ).all()
    ctx = KnowledgeContext.load(session, style.brand_id)
    _docs, source_context = _style_discussion_source_context(session, style)
    prompt = _style_discussion_prompt(
        style,
        ctx,
        source_context,
        _style_discussion_history(messages),
        message,
        discussion.draft_name,
        discussion.draft_summary,
    )
    try:
        raw = llm.generate_text(
            prompt,
            task="style_discussion",
            module="writing",
            fallback=False,
        )
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc

    assistant_content = clean_llm_output(raw)
    draft = _parse_style_discussion_draft(assistant_content)
    if draft is not None:
        discussion.draft_name, discussion.draft_summary, discussion.draft_reason = draft
    discussion.updated_at = _now()
    session.add(StyleDiscussionMessage(
        discussion_id=discussion.id,
        role="assistant",
        content=assistant_content,
    ))
    session.add(discussion)
    session.commit()
    return templates.TemplateResponse(
        request,
        "writing/_style_discussion.html",
        _style_discussion_page_context(request, session, style, discussion),
    )


def _style_discussion_sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.post("/writing/styles/{style_id}/discussion/stream")
def style_discussion_stream(style_id: int, request: Request,
                            message: str = Form(""),
                            session: Session = Depends(get_session)):
    """流式输出风格讨论；完整输出结束后才解析并保存修改草案。"""
    style = _get_style_for_discussion(style_id, request, session)
    message = (message or "").strip()
    if not message:
        raise HTTPException(400, "请输入想讨论的修改方向")
    if len(message) > 4000:
        raise HTTPException(400, "单条消息不能超过 4000 字")

    discussion = _get_or_create_style_discussion(session, style)
    session.add(StyleDiscussionMessage(
        discussion_id=discussion.id,
        role="user",
        content=message,
    ))
    session.commit()
    messages = session.exec(
        select(StyleDiscussionMessage)
        .where(StyleDiscussionMessage.discussion_id == discussion.id)
        .order_by(StyleDiscussionMessage.created_at, StyleDiscussionMessage.id)
    ).all()
    ctx = KnowledgeContext.load(session, style.brand_id)
    _docs, source_context = _style_discussion_source_context(session, style)
    prompt = _style_discussion_prompt(
        style,
        ctx,
        source_context,
        _style_discussion_history(messages),
        message,
        discussion.draft_name,
        discussion.draft_summary,
    )

    def generate_events():
        chunks: list[str] = []
        yield _style_discussion_sse("thinking", {})
        try:
            for chunk in llm.stream_text(
                prompt,
                task="style_discussion",
                module="writing",
                fallback=False,
            ):
                if not chunk:
                    continue
                chunks.append(chunk)
                yield _style_discussion_sse("delta", {"text": chunk})
            assistant_content = clean_llm_output("".join(chunks))
            if not assistant_content:
                raise RuntimeError("AI 没有返回内容")
            draft = _parse_style_discussion_draft(assistant_content)
            if draft is not None:
                discussion.draft_name, discussion.draft_summary, discussion.draft_reason = draft
            discussion.updated_at = _now()
            session.add(StyleDiscussionMessage(
                discussion_id=discussion.id,
                role="assistant",
                content=assistant_content,
            ))
            session.add(discussion)
            session.commit()
            yield _style_discussion_sse("done", {
                "has_draft": bool(discussion.draft_name and discussion.draft_summary),
            })
        except Exception as exc:
            session.rollback()
            yield _style_discussion_sse("error", {"message": str(exc)[:500]})

    return StreamingResponse(
        generate_events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/writing/styles/{style_id}/discussion/apply")
def apply_style_discussion(style_id: int, request: Request,
                           session: Session = Depends(get_session)):
    """把用户确认过的 AI 修改草案应用到原风格，并保存版本记录。"""
    style = _get_style_for_discussion(style_id, request, session)
    discussion = _get_or_create_style_discussion(session, style)
    if not discussion.draft_name or not discussion.draft_summary:
        raise HTTPException(400, "当前还没有可应用的修改草案")
    session.add(StyleRevision(
        style_id=style.id,
        discussion_id=discussion.id,
        previous_name=style.name,
        previous_summary=style.summary,
        new_name=discussion.draft_name,
        new_summary=discussion.draft_summary,
        change_reason=discussion.draft_reason,
    ))
    style.name = discussion.draft_name
    style.summary = discussion.draft_summary
    discussion.draft_name = ""
    discussion.draft_summary = ""
    discussion.draft_reason = ""
    discussion.last_applied_at = _now()
    discussion.updated_at = _now()
    session.add(style)
    session.add(discussion)
    session.commit()
    return RedirectResponse(
        f"/writing?tab=library&highlight={style.id}&style_sync=applied&style_sync_id={style.id}",
        status_code=303,
    )


@router.post("/writing/styles/{style_id}/discussion/save-as")
def save_style_discussion_as_new(style_id: int, request: Request,
                                 session: Session = Depends(get_session)):
    """把 AI 修改草案另存为一个新风格，不改变当前风格。"""
    style = _get_style_for_discussion(style_id, request, session)
    discussion = _get_or_create_style_discussion(session, style)
    if not discussion.draft_name or not discussion.draft_summary:
        raise HTTPException(400, "当前还没有可保存的修改草案")
    new_style = Style(
        brand_id=style.brand_id,
        name=discussion.draft_name,
        summary=discussion.draft_summary,
        reference_url=style.reference_url,
        source="discussion",
        is_default=False,
    )
    session.add(new_style)
    session.commit()
    session.refresh(new_style)
    source_docs = session.exec(
        select(StyleDoc).where(StyleDoc.style_id == style.id)
    ).all()
    for doc in source_docs:
        copied_path = doc.file_path
        if os.path.isfile(doc.file_path):
            extension = os.path.splitext(doc.file_path)[1]
            copied_path = os.path.join(
                os.path.dirname(doc.file_path), f"{uuid.uuid4().hex}{extension}"
            )
            shutil.copy2(doc.file_path, copied_path)
        session.add(StyleDoc(
            brand_id=doc.brand_id,
            style_id=new_style.id,
            filename=doc.filename,
            file_path=copied_path,
            extracted_text=doc.extracted_text,
            note=doc.note,
        ))
    if source_docs:
        session.commit()
    return RedirectResponse(
        f"/writing?tab=library&highlight={new_style.id}&style_sync=created&style_sync_id={new_style.id}",
        status_code=303,
    )


@router.post("/writing/styles/extract")
def extract_style(request: Request, url: str = Form(...),
                  session: Session = Depends(get_session)):
    """新建·URL 提取：抓 URL 页面正文 → LLM 提炼写作风格。"""
    auth.require_level(request, 1)
    brand = _first_brand(session)
    if brand is None or brand.id is None:
        raise HTTPException(404, "品牌不存在")
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "请输入完整的 URL（以 http:// 或 https:// 开头）")
    try:
        text = _fetch_url_text(url)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"抓取 URL 失败：{exc}") from exc
    if not text.strip():
        raise HTTPException(502, "该页面未提取到正文内容")
    try:
        raw = llm.generate_text(_extract_style_prompt(url, text),
                                task="writing_style_extract", module="writing", fallback=False)
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    parsed = _parse_styles(raw)
    if not parsed:
        raise HTTPException(502, "AI 未能从该页面提炼出风格")
    name, summary = parsed[0]
    existing_default = _default_style(session, brand.id) is not None
    style = Style(
        brand_id=brand.id, name=name, summary=summary,
        reference_url=url, source="url",
        is_default=not existing_default,
    )
    session.add(style)
    session.commit()
    session.refresh(style)
    return RedirectResponse(f"/writing?tab=library&highlight={style.id}", status_code=303)


@router.post("/writing/styles/manual")
async def manual_style(request: Request,
                       note: str = Form(""),
                       files: list[UploadFile] = File([]),
                       session: Session = Depends(get_session)):
    """新建·手动上传：上传本地文档（pdf/word/ppt/xlsx/txt 等，可多选）+ 可选文字说明
    → 抽文本喂 LLM 提炼写作风格 → 落入风格库（source=manual）。

    - 文件和文字至少有一项；同时存在时，文字作为这些文件的说明注入 prompt。
    - 上传原文件持久化保留（存入 StyleDoc 记录），供历史回溯查看。
    """
    auth.require_level(request, 1)
    brand = _first_brand(session)
    if brand is None or brand.id is None:
        raise HTTPException(404, "品牌不存在")
    note = (note or "").strip()
    # 过滤空文件名占位（浏览器多文件框未选时会发一个空 UploadFile）
    uploads = [f for f in files if f.filename]
    if not uploads and not note:
        raise HTTPException(400, "请上传文件或填写文字说明")
    # 抽文本：每文件单独抽，按文件名顺序拼接；同时落盘持久化
    text_parts: list[str] = []
    filenames: list[str] = []
    saved_docs: list[dict] = []  # [{path, filename, text}, ...]
    for f in uploads:
        fn = f.filename or "未命名"
        filenames.append(fn)
        path = storage.save_upload(f, subdir=f"writing/style-manual/{brand.id}")
        t = docparse.extract_text(path).strip()
        saved_docs.append({"path": path, "filename": fn, "text": t})
        if t:
            text_parts.append(t)
    text = "\n\n".join(text_parts)
    # 无文件或文件都抽不出文本 → 回退用文字说明本身作为待分析正文
    if not text:
        if not note:
            raise HTTPException(
                400, "未能从上传文件中抽出文本（可能是扫描件 PDF），请补填文字说明")
        text = note
    try:
        raw = llm.generate_text(_manual_style_prompt(filenames, note, text),
                                task="writing_style_manual", module="writing", fallback=False)
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    parsed = _parse_styles(raw)
    if not parsed:
        raise HTTPException(502, "AI 未能从该内容提炼出风格")
    name, summary = parsed[0]
    existing_default = _default_style(session, brand.id) is not None
    style = Style(
        brand_id=brand.id, name=name, summary=summary,
        reference_url="", source="manual",
        is_default=not existing_default,
    )
    session.add(style)
    session.commit()
    session.refresh(style)
    # 持久化上传文档历史记录
    for doc in saved_docs:
        session.add(StyleDoc(
            brand_id=brand.id, style_id=style.id,
            filename=doc["filename"], file_path=doc["path"],
            extracted_text=doc["text"], note=note,
        ))
    session.commit()
    return RedirectResponse(f"/writing?tab=library&highlight={style.id}", status_code=303)


@router.get("/writing/styles/docs/{doc_id}/download")
def download_style_doc(doc_id: int, session: Session = Depends(get_session)):
    """下载手动上传的风格文档原文件。"""
    doc = session.get(StyleDoc, doc_id)
    if doc is None or not os.path.exists(doc.file_path):
        raise HTTPException(404, "文档不存在")
    return FileResponse(doc.file_path, filename=doc.filename)


@router.post("/writing/styles/docs/{doc_id}/delete")
def delete_style_doc(doc_id: int, request: Request,
                     session: Session = Depends(get_session)):
    """删除手动上传的历史文档记录及磁盘文件。"""
    auth.require_level(request, 1)
    doc = session.get(StyleDoc, doc_id)
    if doc is None:
        raise HTTPException(404, "文档不存在")
    shared_path = session.exec(
        select(StyleDoc.id).where(
            StyleDoc.file_path == doc.file_path,
            StyleDoc.id != doc.id,
        )
    ).first()
    if shared_path is None:
        try:
            os.remove(doc.file_path)
        except OSError:
            pass
    session.delete(doc)
    session.commit()
    return RedirectResponse("/writing?tab=new", status_code=303)


@router.post("/writing/styles/reextract")
async def reextract_style(request: Request,
                          note: str = Form(""),
                          doc_ids: list[int] = Form([]),
                          session: Session = Depends(get_session)):
    """从历史文档中重新提炼风格：选中已上传的 StyleDoc → 取 extracted_text → 喂 LLM 提炼。
    不新建 StyleDoc（同一文档可多次提炼不同风格），只创建新的 Style 记录。
    """
    auth.require_level(request, 1)
    brand = _first_brand(session)
    if brand is None or brand.id is None:
        raise HTTPException(404, "品牌不存在")
    note = (note or "").strip()
    if not doc_ids:
        raise HTTPException(400, "请至少选择一个历史文档")
    docs = session.exec(
        select(StyleDoc).where(
            StyleDoc.id.in_(doc_ids),
            StyleDoc.brand_id == brand.id,
        ).order_by(StyleDoc.id)
    ).all()
    if not docs:
        raise HTTPException(404, "选中的文档不存在")
    # 拼接抽出文本 + 文件名
    text_parts: list[str] = []
    filenames: list[str] = []
    for doc in docs:
        filenames.append(doc.filename)
        if doc.extracted_text:
            text_parts.append(doc.extracted_text)
    text = "\n\n".join(text_parts)
    if not text:
        if not note:
            raise HTTPException(
                400, "选中的文档没有抽出文本（可能是扫描件 PDF），请补填文字说明")
        text = note
    try:
        raw = llm.generate_text(_manual_style_prompt(filenames, note, text),
                                task="writing_style_manual", module="writing", fallback=False)
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    parsed = _parse_styles(raw)
    if not parsed:
        raise HTTPException(502, "AI 未能从该内容提炼出风格")
    name, summary = parsed[0]
    existing_default = _default_style(session, brand.id) is not None
    style = Style(
        brand_id=brand.id, name=name, summary=summary,
        reference_url="", source="manual",
        is_default=not existing_default,
    )
    session.add(style)
    session.commit()
    session.refresh(style)
    return RedirectResponse(f"/writing?tab=library&highlight={style.id}", status_code=303)


@router.post("/writing/reqs/save")
def save_writing_req(request: Request,
                     content: str = Form(""),
                     session: Session = Depends(get_session)):
    """保存写作要求到提示词库（品牌级，可复用）。"""
    auth.require_level(request, 1)
    brand = _first_brand(session)
    if brand is None or brand.id is None:
        raise HTTPException(404, "品牌不存在")
    content = (content or "").strip()
    if not content:
        raise HTTPException(400, "写作要求内容不能为空")

    # 写作要求按品牌复用；首尾空白在上面已统一去除，因此同一条要求
    # 重复点击保存或从不同入口保存时，都不会新增重复记录。
    existing = session.exec(
        select(WritingReq).where(
            WritingReq.brand_id == brand.id,
            WritingReq.content == content,
        )
    ).first()
    if existing is not None:
        return RedirectResponse("/writing", status_code=303)

    req = WritingReq(brand_id=brand.id, content=content)
    session.add(req)
    try:
        session.commit()
    except IntegrityError:
        # 唯一索引兜底并发请求：另一请求可能在查询后已经先提交。
        session.rollback()
        return RedirectResponse("/writing", status_code=303)
    session.refresh(req)
    return RedirectResponse("/writing", status_code=303)


@router.post("/writing/reqs/{req_id}/delete")
def delete_writing_req(req_id: int, request: Request,
                       session: Session = Depends(get_session)):
    """删除已保存的写作要求。"""
    auth.require_level(request, 1)
    req = session.get(WritingReq, req_id)
    if req is None:
        raise HTTPException(404, "写作要求不存在")
    session.delete(req)
    session.commit()
    return RedirectResponse("/writing", status_code=303)


@router.post("/writing/topics/{topic_id}/generate")
def generate_article(topic_id: int, request: Request,
                      debate_rounds: int = Form(0), review_rounds: int = Form(0),
                      platform: str = Form(""), word_count: int = Form(0),
                      ai_images: str = Form(""), use_experience: str = Form(""),
                      writing_req: str = Form(""),
                      session: Session = Depends(get_session)):
    """生成图文（异步后台）：辩论 → 生成文本 → 多插图候选 → 评审 → 重写 → 待审核。

    HTMX 请求：返回替换当前选题卡片的进度片段，页面不跳转、用户原地看辩论。
    普通请求：重定向回 /writing。
    """
    auth.require_level(request, 1)
    topic = session.get(Topic, topic_id)
    if topic is None:
        raise HTTPException(404, "选题不存在")
    if topic.status != "采纳":
        raise HTTPException(400, "只有已采纳选题可以进入写作")
    dr = max(0, min(debate_rounds, 5))
    rr = max(0, min(review_rounds, 5))
    pf = platform.strip() if platform and platform.strip() in PLATFORMS else ""
    wc = max(0, min(word_count, 10000))
    # checkbox 勾选才发送 ai_images=true，未勾选时字段缺失（Form("") 兜底）→ False
    ai_images_flag = ai_images.strip().lower() in ("true", "1", "yes", "on")
    # Campaign 总体经验包默认纳入后续生成；只有明确传 false/off/no/0 才关闭，便于兼容旧表单。
    use_experience_flag = use_experience.strip().lower() not in ("false", "0", "no", "off")
    wr = writing_req.strip() if writing_req else ""
    # 同选题已有生成中的图文（无错误）→ 阻止并提示，不新建
    running = session.exec(
        _active_article_query()
        .where(Article.topic_id == topic.id, Article.status.in_(RUNNING_ARTICLE_STATUSES))
        .order_by(Article.updated_at.desc())
    ).first()
    if running and not running.error_message:
        if request.headers.get("HX-Request") == "true":
            all_articles = session.exec(
                _active_article_query()
                .where(Article.topic_id == topic.id)
                .order_by(Article.updated_at.desc(), Article.id.desc())
            ).all()
            campaigns = session.exec(select(Campaign).where(Campaign.brand_id == topic.brand_id)).all()
            ds = _default_style(session, topic.brand_id)
            wrs = session.exec(
                select(WritingReq).where(WritingReq.brand_id == topic.brand_id)
                .order_by(WritingReq.created_at.desc(), WritingReq.id.desc())
            ).all() if topic.brand_id else []
            return templates.TemplateResponse(request, "writing/_topic_card.html", {
                "request": request,
                "t": topic,
                "topic": topic,
                "campaigns": campaigns,
                "cmap": {c.id: c.name for c in campaigns},
                "campaign_name": {c.id: c.name for c in campaigns}.get(topic.campaign_id, "品牌常青"),
                "level": getattr(request.state, "level", 0),
                "topic_articles": {topic.id: all_articles},
                "platforms": PLATFORMS,
                "default_style": ds,
                "writing_reqs": wrs,
                "block_message": "当前已有生成中的图文，请等待完成后再生成新的。",
            })
        return RedirectResponse("/writing", status_code=303)
    # 每次生成都是新建一篇图文（支持一选题多图文，旧的保留不动）
    llm_provider, llm_model = llm.text_model_info("writing")
    article = Article(
        topic_id=topic.id, campaign_id=topic.campaign_id, title=topic.title,
        status="辩论中" if dr > 0 else "写作中",
        debate_rounds=dr, review_rounds=rr, platform=pf, word_count=wc,
        llm_provider=llm_provider, llm_model=llm_model,
        writing_req=wr,
        updated_at=_now(),
    )
    session.add(article)
    session.commit()
    session.refresh(article)
    # 后台线程跑完整流程
    t = threading.Thread(
        target=_run_generation_worker,
        args=(article.id, topic.id, dr, rr, pf, wc, ai_images_flag, use_experience_flag, wr),
        daemon=True,
    )
    t.start()

    if request.headers.get("HX-Request") == "true":
        # HTMX：左侧刷新原选题卡片（显示新+旧图文列表），右侧 OOB 追加生成中卡片
        campaigns = session.exec(select(Campaign).where(Campaign.brand_id == topic.brand_id)).all()
        all_articles = session.exec(
            _active_article_query()
            .where(Article.topic_id == topic.id)
            .order_by(Article.updated_at.desc(), Article.id.desc())
        ).all()
        ds = _default_style(session, topic.brand_id)
        wrs = session.exec(
            select(WritingReq).where(WritingReq.brand_id == topic.brand_id)
            .order_by(WritingReq.created_at.desc(), WritingReq.id.desc())
        ).all() if topic.brand_id else []
        return templates.TemplateResponse(request, "writing/_generate_response.html", {
            "request": request,
            "t": topic,
            "topic": topic,
            "article": article,
            "campaigns": campaigns,
            "cmap": {c.id: c.name for c in campaigns},
            "campaign_name": {c.id: c.name for c in campaigns}.get(topic.campaign_id, "品牌常青"),
            "level": getattr(request.state, "level", 0),
            "topic_articles": {topic.id: all_articles},
            "records": [],
            "oob": True,
            "default_style": ds,
            "writing_reqs": wrs,
            "display_phase": None,
            "platforms": PLATFORMS,
        })
    return RedirectResponse("/writing", status_code=303)


def _run_generation_worker(article_id: int, topic_id: int, debate_rounds: int, review_rounds: int,
                           platform: str = "", word_count: int = 0, ai_images: bool = True,
                           use_experience: bool = True, writing_req: str = "") -> None:
    """后台线程：辩论 → 生成文本 → 评审 → 重写 → 待配图（提交正文，可阅读）。

    文本完成后立即把状态置为「待配图」并启动配图子线程，不阻塞用户阅读正文。
    独立 Session，失败写 error_message。
    """
    from sqlmodel import Session as SMSession
    with SMSession(db.engine) as s:
        try:
            article = s.get(Article, article_id)
            topic = s.get(Topic, topic_id)
            if article is None or topic is None:
                return
            ctx = KnowledgeContext.load(s, topic.brand_id, topic.campaign_id)
            style = _default_style(s, topic.brand_id)
            style_text = style.summary if style else "无默认风格，使用品牌内容要求。"
            writing_experience = (
                campaign_experience_context(
                    s,
                    topic.brand_id,
                    topic.campaign_id,
                    platform=platform,
                    task="writing",
                    inherited_packs=ctx.pool_experiences,
                )
                if use_experience else ""
            )

            # ── 辩论阶段 ──
            if debate_rounds > 0:
                brief = run_debate(s, article_id, debate_rounds, topic, ctx, writing_experience)
                article.debate_brief = brief
                article.updated_at = _now()
                s.add(article)
                s.commit()
                prompt = _article_prompt_with_brief(
                    topic, ctx, style, brief, platform, word_count, writing_experience, writing_req)
            else:
                prompt = _article_prompt(topic, ctx, style, platform, word_count, writing_experience, writing_req)

            # ── 生成文本 ──
            llm_provider, llm_model = llm.text_model_info("writing")
            article.status = "写作中"
            article.llm_provider = llm_provider
            article.llm_model = llm_model
            article.updated_at = _now()
            s.add(article)
            s.commit()
            body = llm.generate_text(prompt, task="writing_article", module="writing", fallback=False)
            body = clean_llm_output(body)
            if not body:
                raise RuntimeError("文本 provider 返回空文章")

            article.style_id = style.id if style else None
            article.title = _article_title(body, topic.title)
            article.body = body
            s.add(article)
            s.commit()
            s.refresh(article)

            # ── 评审阶段 ──
            if review_rounds > 0:
                review_summary = run_review(s, article_id, review_rounds, article)
                article.review_summary = review_summary
                article.status = "重写中"
                article.updated_at = _now()
                s.add(article)
                s.commit()
                # 按评审建议重写
                rewrite_p = rewrite_prompt(article, review_summary, topic, ctx, style_text, writing_experience, writing_req)
                new_body = llm.generate_text(rewrite_p, task="writing_rewrite", module="writing", fallback=False)
                new_body = clean_llm_output(new_body)
                if new_body:
                    article.body = new_body
                    article.title = _article_title(new_body, topic.title)
                    s.add(article)
                    s.commit()
                    s.refresh(article)

            # ── 文本完成：AI 配图→「待配图」并启动子线程；自配图→直接「待审核」等用户上传 ──
            article.status = "待配图" if ai_images else "待审核"
            article.error_message = ""
            article.generated_at = _now()
            article.updated_at = article.generated_at
            s.add(article)
            s.commit()

            # AI 配图：启动子线程异步生成多插图候选
            if ai_images:
                t = threading.Thread(
                    target=_run_image_worker,
                    args=(article_id, topic_id, platform),
                    daemon=True,
                )
                t.start()

        except Exception as exc:
            s.rollback()
            article = s.get(Article, article_id)
            if article:
                article.status = "待审核" if article.body else "写作中"
                article.error_message = str(exc)[:500]
                article.updated_at = _now()
                s.add(article)
                s.commit()


def _run_image_worker(article_id: int, topic_id: int, platform: str = "",
                      missing_only: bool = False) -> None:
    """配图子线程：为已生成的正文生成多插图候选(4张/位置) → 待审核/待配图。

    独立 Session，失败写 error_message（不影响已完成的正文）。
    missing_only=True：只补生候选图不足 4 张的 slot，不清理已有图（用于「补生缺失配图」）。
    每 slot 用 minimax n 参数批量生成（1 次 API 调用出 4 张，而非 4 次串行调用）。
    """
    lock = _get_article_lock(article_id)
    if not lock.acquire(blocking=False):
        return  # 该文章已有配图 worker 在跑，跳过
    try:
        from sqlmodel import Session as SMSession
        with SMSession(db.engine) as s:
            try:
                article = s.get(Article, article_id)
                topic = s.get(Topic, topic_id)
                if article is None or topic is None:
                    return
                if not article.body:
                    return  # 没正文，没法配图
                ctx = KnowledgeContext.load(s, topic.brand_id, topic.campaign_id)
                style = _default_style(s, topic.brand_id)

                slots = _parse_image_slots(article.body)
                if not slots:
                    slots = [(0, topic.image_hint or "文章配图")]

                if missing_only:
                    # 补生模式：只处理候选图不足 4 张的 slot，不清理已有图
                    existing = s.exec(
                        select(ArticleImage).where(ArticleImage.article_id == article_id)
                    ).all()
                    existing_count: dict[int, int] = {}
                    for img in existing:
                        existing_count[img.slot_index] = existing_count.get(img.slot_index, 0) + 1
                else:
                    # 全量模式：清理旧候选图
                    old_imgs = s.exec(select(ArticleImage).where(ArticleImage.article_id == article_id)).all()
                    for oi in old_imgs:
                        s.delete(oi)
                    s.commit()
                    existing_count = {}

                for slot_idx, (_, slot_desc) in enumerate(slots):
                    if missing_only:
                        cur_count = existing_count.get(slot_idx, 0)
                        # 手动上传 slot（用户已上传图）不补 AI 候选
                        slot_imgs = [im for im in existing if im.slot_index == slot_idx]
                        if slot_imgs and all(im.prompt == "手动上传" for im in slot_imgs):
                            continue
                        if cur_count >= 4:
                            continue  # 该 slot 已满 4 张，跳过
                        need = 4 - cur_count
                    else:
                        need = 4
                    img_p = _image_prompt_for_slot(topic, ctx, style, slot_desc, article.body, platform)
                    image_provider, image_model = llm.image_model_info("writing")
                    try:
                        urls = llm.generate_images(img_p, module="writing", n=need, fallback=False)
                    except RuntimeError:
                        continue  # 该 slot 批量生成失败，跳过（不中断其他 slot）
                    for candidate_idx, url in enumerate(urls):
                        # 补生模式下，如果该 slot 已有选中图，新图不选中；否则第一张选中
                        if missing_only and cur_count > 0:
                            is_sel = False
                        else:
                            is_sel = (candidate_idx == 0)
                        s.add(ArticleImage(
                            article_id=article_id, prompt=img_p, image_url=_public_image_url(url),
                            slot_index=slot_idx, slot_desc=slot_desc,
                            is_selected=is_sel,
                            image_provider=image_provider,
                            image_model=image_model,
                        ))
                    s.commit()

                # 所有 slot 满 4 张 → 待审核（默认选中 idx0，用户可换选）；
                # 有 slot 不足 4 张（部分失败）→ 待配图，让用户点「补生缺失配图」。
                all_imgs = s.exec(
                    select(ArticleImage).where(ArticleImage.article_id == article_id)
                ).all()
                article.status = "待审核" if _all_image_slots_full(article.body, all_imgs) else "待配图"
                article.error_message = ""
                article.updated_at = _now()
                s.add(article)
                s.commit()

            except Exception as exc:
                s.rollback()
                article = s.get(Article, article_id)
                if article:
                    # 配图失败但正文已完成：回到「待审核」让用户查阅，错误记在 error_message
                    article.status = "待审核"
                    article.error_message = f"配图生成失败：{str(exc)[:400]}"
                    article.updated_at = _now()
                    s.add(article)
                    s.commit()
    finally:
        lock.release()


def _run_slot_batch_worker(article_id: int, topic_id: int,
                           slot_specs: list[tuple[int, str, str]],
                           platform: str = "",
                           replace_after_generation: bool = True) -> None:
    """配图子线程：串行完成一批 slot，再统一结算文章状态。

    同一文章的多个插图不能各自启动 worker：单个 worker 完成时，其他 slot
    可能仍保留 4 张旧图，按候选图数量判断会把文章错误地提前标成「待审核」。
    这里由一个 worker 持有文章锁，逐个处理整批 slot，所有 slot 完成后才结算
    「待审核/待配图」状态。新候选图不足 4 张时不替换旧图，保证可以回退。

    ``slot_specs`` 中每项为 ``(slot_index, slot_desc, prompt_override)``。
    """
    lock = _get_article_lock(article_id)
    lock.acquire()
    try:
        from sqlmodel import Session as SMSession
        with SMSession(db.engine) as s:
            article = None
            errors: list[str] = []
            try:
                article = s.get(Article, article_id)
                topic = s.get(Topic, topic_id)
                if article is None or topic is None:
                    return
                if not article.body:
                    return
                ctx = KnowledgeContext.load(s, topic.brand_id, topic.campaign_id)
                style = _default_style(s, topic.brand_id)

                for slot_index, slot_desc, prompt_override in slot_specs:
                    old_imgs = s.exec(
                        select(ArticleImage).where(
                            ArticleImage.article_id == article_id,
                            ArticleImage.slot_index == slot_index,
                        )
                    ).all()
                    img_p = (prompt_override or "").strip() or _image_prompt_for_slot(
                        topic, ctx, style, slot_desc, article.body, platform)
                    image_provider, image_model = llm.image_model_info("writing")
                    try:
                        urls = llm.generate_images(img_p, module="writing", n=4, fallback=False)
                        urls = list(urls or [])
                        if replace_after_generation and len(urls) < 4:
                            raise RuntimeError(
                                f"图片服务只返回 {len(urls)} 张候选图，需要完整返回 4 张后再替换"
                            )
                        # 只有候选图准备好后才删除旧图；部分返回或异常均可回退。
                        for oi in old_imgs:
                            s.delete(oi)
                        for candidate_idx, url in enumerate(urls[:4]):
                            # 默认选中第 0 张：AI 给默认选择，用户不换 = 默认认可
                            s.add(ArticleImage(
                                article_id=article_id, prompt=img_p, image_url=_public_image_url(url),
                                slot_index=slot_index, slot_desc=slot_desc,
                                is_selected=(candidate_idx == 0),
                                image_provider=image_provider,
                                image_model=image_model,
                            ))
                        s.commit()
                    except Exception as exc:
                        s.rollback()
                        errors.append(f"插图位置 {slot_index + 1}：{str(exc)[:180]}")
                        continue

                # 只有整批 slot 都处理完后才统一结算状态，避免第一项完成时提前进入待审核。
                article = s.get(Article, article_id)
                if article is None:
                    return
                all_imgs = s.exec(
                    select(ArticleImage).where(ArticleImage.article_id == article_id)
                ).all()
                if errors and len(slot_specs) == 1 and replace_after_generation:
                    # 保持单 slot 旧逻辑：即使旧候选图不足 4 张，失败时也回到待审核供用户查看回退图。
                    article.status = "待审核"
                elif _all_image_slots_full(article.body, all_imgs):
                    article.status = "待审核"
                else:
                    article.status = "待配图"
                article.error_message = "；".join(errors)[:400]
                article.image_generation_slot = -1
                article.updated_at = _now()
                s.add(article)
                s.commit()

            except Exception as exc:
                s.rollback()
                article = s.get(Article, article_id)
                if article:
                    article.status = "待审核"
                    article.error_message = f"插图候选生成失败：{str(exc)[:400]}"
                    article.image_generation_slot = -1
                    article.updated_at = _now()
                    s.add(article)
                    s.commit()
                print(f"[slot-batch-worker] article={article_id} 失败: {exc}", flush=True)
    finally:
        lock.release()


def _run_single_slot_worker(article_id: int, topic_id: int, slot_index: int,
                             slot_desc: str, platform: str = "",
                             prompt_override: str = "",
                             replace_after_generation: bool = False) -> None:
    """兼容单 slot 调用，实际仍走批处理 worker 的统一结算逻辑。"""
    _run_slot_batch_worker(
        article_id,
        topic_id,
        [(slot_index, slot_desc, prompt_override)],
        platform,
        replace_after_generation,
    )


def _display_phase_for_article(article: Article) -> str | None:
    """根据 article 当前持久化状态推导页面应展示的阶段标签。"""
    if article.status in ("辩论中", "写作中", "重写中", "AI审核中"):
        return article.status
    if article.status == "待审核" and article.review_rounds > 0 and not article.review_summary:
        return "评审中"
    # 待配图/待审核/已排期/已发布 → None（待配图/待审核单独处理选图界面）
    return None


def _article_list_item_fragment(request: Request, article: Article, topic: Topic,
                                campaigns: list[Campaign]):
    """返回文章库简洁列表项片段（列表轮询用）。"""
    cmap = {c.id: c.name for c in campaigns}
    return templates.TemplateResponse(request, "writing/_article_list_item.html", {
        "request": request,
        "article": article,
        "topic": topic,
        "campaign_name": cmap.get(topic.campaign_id, "品牌常青") if topic else "",
        "level": getattr(request.state, "level", 0),
    })


def _article_detail_fragment(request: Request, article: Article, topic: Topic,
                              campaigns: list[Campaign], session: Session,
                              force_editing: bool = False):
    """返回文章详情内容片段（详情页轮询用）：辩论过程 / 选图 / 最终文章。

    force_editing=True 时片段以编辑模式初始渲染（用于 edit-body/insert-image 后保持编辑状态）。
    """
    cmap = {c.id: c.name for c in campaigns}
    ctx = {
        "request": request,
        "article": article,
        "topic": topic,
        "campaign_name": cmap.get(topic.campaign_id, "品牌常青") if topic else "",
        "level": getattr(request.state, "level", 0),
        "records": [],
        "display_phase": None,
        "slots": {},
        "has_pending_images": False,
        "all_slots_selected": False,
        "has_missing_slots": False,
        "article_body_clean": "",
        "body_segments": [],
        "force_editing": force_editing,
        "image_generation_slot": getattr(article, "image_generation_slot", -1),
    }
    # 待配图 / 待审核：装载全部候选图（供换选/重生）+ 图文混排切片
    if article.status in ("待配图", "待审核"):
        images = session.exec(
            select(ArticleImage).where(ArticleImage.article_id == article.id)
            .order_by(ArticleImage.slot_index, ArticleImage.id)
        ).all()
        # 自动恢复卡住的配图 worker（待配图 + 超 5 分钟无更新 + 有缺失 slot）
        if _maybe_resume_stalled_image_worker(article, images):
            session.refresh(article)
        slots: dict[int, list[ArticleImage]] = {}
        for img in images:
            slots.setdefault(img.slot_index, []).append(img)
        ctx["slots"] = slots
        ctx["has_pending_images"] = _has_pending_image_candidates(article.body, slots)
        ctx["article_body_clean"] = _strip_image_slots(article.body)
        ctx["body_segments"] = _split_body_by_slots(article.body, slots)
        ctx["all_slots_selected"] = _all_slots_selected(article.body, images)
        ctx["has_missing_slots"] = _has_missing_slots(article.body, images)
        # 待审核也装载辩论/评审记录供查阅
        ctx["records"] = session.exec(
            select(DebateRecord).where(DebateRecord.article_id == article.id)
            .order_by(DebateRecord.round_num, DebateRecord.id)
        ).all()
        return templates.TemplateResponse(request, "writing/_article_detail_content.html", ctx)

    # 正在生成：装载辩论记录
    display_phase = _display_phase_for_article(article)
    if display_phase is not None:
        ctx["records"] = session.exec(
            select(DebateRecord).where(DebateRecord.article_id == article.id)
            .order_by(DebateRecord.round_num, DebateRecord.id)
        ).all()
        ctx["display_phase"] = display_phase
        # AI审核中：额外装载选中图 + 正文切片，供展示被审核的图文
        if article.status == "AI审核中":
            selected_imgs = session.exec(
                select(ArticleImage).where(
                    ArticleImage.article_id == article.id,
                    ArticleImage.is_selected == True,
                ).order_by(ArticleImage.slot_index)
            ).all()
            slots: dict[int, list[ArticleImage]] = {}
            for img in selected_imgs:
                slots.setdefault(img.slot_index, []).append(img)
            ctx["slots"] = slots
            ctx["body_segments"] = _split_body_by_slots(article.body, slots)
        return templates.TemplateResponse(request, "writing/_article_detail_content.html", ctx)

    # 已审核/审核未通过/已排期/已发布：装载辩论/评审记录 + 选中的图用于图文混排展示（只读）
    ctx["records"] = session.exec(
        select(DebateRecord).where(DebateRecord.article_id == article.id)
        .order_by(DebateRecord.round_num, DebateRecord.id)
    ).all()
    selected_imgs = session.exec(
        select(ArticleImage).where(
            ArticleImage.article_id == article.id,
            ArticleImage.is_selected == True,
        ).order_by(ArticleImage.slot_index)
    ).all()
    slots: dict[int, list[ArticleImage]] = {}
    for img in selected_imgs:
        slots.setdefault(img.slot_index, []).append(img)
    ctx["slots"] = slots
    ctx["body_segments"] = _split_body_by_slots(article.body, slots)
    return templates.TemplateResponse(request, "writing/_article_detail_content.html", ctx)


@router.get("/writing/articles/{article_id}")
def article_detail(article_id: int, request: Request, session: Session = Depends(get_session)):
    """文章详情页（新窗口）：展示实时辩论过程 / 选图 / 最终文章。"""
    article = session.get(Article, article_id)
    if article is None:
        raise HTTPException(404, "文章不存在")
    topic = session.get(Topic, article.topic_id)
    campaigns = session.exec(select(Campaign).where(Campaign.brand_id == topic.brand_id)).all() if topic else []
    cmap = {c.id: c.name for c in campaigns}
    ctx = {
        "request": request,
        "article": article,
        "topic": topic,
        "campaign_name": cmap.get(topic.campaign_id, "品牌常青") if topic else "",
        "level": getattr(request.state, "level", 0),
        "records": [],
        "display_phase": None,
        "slots": {},
        "has_pending_images": False,
        "article_body_clean": "",
        "body_segments": [],
        "force_editing": False,
        "image_generation_slot": getattr(article, "image_generation_slot", -1),
    }
    if article.status in ("待配图", "待审核"):
        images = session.exec(
            select(ArticleImage).where(ArticleImage.article_id == article.id)
            .order_by(ArticleImage.slot_index, ArticleImage.id)
        ).all()
        # 自动恢复卡住的配图 worker（待配图 + 超 5 分钟无更新 + 有缺失 slot）
        if _maybe_resume_stalled_image_worker(article, images):
            session.refresh(article)
        slots: dict[int, list[ArticleImage]] = {}
        for img in images:
            slots.setdefault(img.slot_index, []).append(img)
        ctx["slots"] = slots
        ctx["has_pending_images"] = _has_pending_image_candidates(article.body, slots)
        ctx["article_body_clean"] = _strip_image_slots(article.body)
        ctx["body_segments"] = _split_body_by_slots(article.body, slots)
        ctx["all_slots_selected"] = _all_slots_selected(article.body, images)
        ctx["has_missing_slots"] = _has_missing_slots(article.body, images)
        # 待审核也装载辩论/评审记录供查阅
        ctx["records"] = session.exec(
            select(DebateRecord).where(DebateRecord.article_id == article.id)
            .order_by(DebateRecord.round_num, DebateRecord.id)
        ).all()
    elif _display_phase_for_article(article) is not None:
        ctx["records"] = session.exec(
            select(DebateRecord).where(DebateRecord.article_id == article.id)
            .order_by(DebateRecord.round_num, DebateRecord.id)
        ).all()
        ctx["display_phase"] = _display_phase_for_article(article)
        # AI审核中：额外装载选中图 + 正文切片，供展示被审核的图文
        if article.status == "AI审核中":
            selected_imgs = session.exec(
                select(ArticleImage).where(
                    ArticleImage.article_id == article.id,
                    ArticleImage.is_selected == True,
                ).order_by(ArticleImage.slot_index)
            ).all()
            slots: dict[int, list[ArticleImage]] = {}
            for img in selected_imgs:
                slots.setdefault(img.slot_index, []).append(img)
            ctx["slots"] = slots
            ctx["body_segments"] = _split_body_by_slots(article.body, slots)
    else:
        # 已排期/已发布：装载辩论/评审记录 + 选中的图用于图文混排展示（只读）
        ctx["records"] = session.exec(
            select(DebateRecord).where(DebateRecord.article_id == article.id)
            .order_by(DebateRecord.round_num, DebateRecord.id)
        ).all()
        selected_imgs = session.exec(
            select(ArticleImage).where(
                ArticleImage.article_id == article.id,
                ArticleImage.is_selected == True,
            ).order_by(ArticleImage.slot_index)
        ).all()
        slots: dict[int, list[ArticleImage]] = {}
        for img in selected_imgs:
            slots.setdefault(img.slot_index, []).append(img)
        ctx["slots"] = slots
        ctx["body_segments"] = _split_body_by_slots(article.body, slots)
    return templates.TemplateResponse(request, "writing/article_detail.html", ctx)


@router.get("/writing/articles/{article_id}/ai-edit/page")
def ai_edit_page(article_id: int, request: Request,
                 session: Session = Depends(get_session)):
    """AI 修改工作台：独立页面承载选区上下文、交互记录和修改预览。"""
    article = _require_ai_edit_article(article_id, request, session)
    topic = session.get(Topic, article.topic_id)
    if topic is None:
        raise HTTPException(404, "来源选题不存在")
    campaigns = session.exec(
        select(Campaign).where(Campaign.brand_id == topic.brand_id)
    ).all()
    campaign_name = {c.id: c.name for c in campaigns}.get(
        topic.campaign_id, "品牌常青")
    workbench = {
        "articleId": article.id,
        "articleUrl": f"/writing/articles/{article.id}",
        "title": article.title or "",
        "body": article.body or "",
        "topicTitle": topic.title or "",
        "campaignName": campaign_name,
    }
    # <script> / Alpine 表达式中不直接插入可执行的用户内容。
    workbench_json = json.dumps(workbench, ensure_ascii=False).replace("<", "\\u003c")
    return templates.TemplateResponse(request, "writing/ai_edit.html", {
        "request": request,
        "article": article,
        "topic": topic,
        "campaign_name": campaign_name,
        "workbench_json": workbench_json,
    })


@router.get("/writing/articles/{article_id}/generate-status")
def generate_status(article_id: int, request: Request, session: Session = Depends(get_session)):
    """HTMX 轮询（列表用）：返回简洁列表项，正在生成的继续轮询，已完成的停止。"""
    article = session.get(Article, article_id)
    if article is None:
        return RedirectResponse("/writing", status_code=303)
    topic = session.get(Topic, article.topic_id)
    if topic is None:
        return RedirectResponse("/writing", status_code=303)
    campaigns = session.exec(select(Campaign).where(Campaign.brand_id == topic.brand_id)).all()
    return _article_list_item_fragment(request, article, topic, campaigns)


@router.get("/writing/articles/{article_id}/detail-status")
def detail_status(article_id: int, request: Request, force_editing: bool = False,
                  session: Session = Depends(get_session)):
    """HTMX 轮询（详情页用）：辩论/写作/重写中 → 更新辩论过程；待配图 → 选图界面；待审核 → 最终文章+换选。"""
    article = session.get(Article, article_id)
    if article is None:
        return RedirectResponse("/writing", status_code=303)
    topic = session.get(Topic, article.topic_id)
    if topic is None:
        return RedirectResponse("/writing", status_code=303)
    campaigns = session.exec(select(Campaign).where(Campaign.brand_id == topic.brand_id)).all()
    return _article_detail_fragment(request, article, topic, campaigns, session,
                                     force_editing=force_editing)


@router.get("/writing/uploads/{rel_path:path}")
def writing_upload(rel_path: str, request: Request):
    """读取写作模块上传图片。走应用登录中间件，不直接暴露整个 data 目录。"""
    if not auth.can_view_module(auth.current_role(request), "writing"):
        raise HTTPException(status_code=403, detail="权限不足")
    root = os.path.realpath(config.DATA_DIR)
    path = os.path.realpath(os.path.join(config.DATA_DIR, rel_path))
    if not (path == root or path.startswith(root + os.sep)):
        raise HTTPException(404, "文件不存在")
    if not os.path.exists(path) or not os.path.isfile(path):
        raise HTTPException(404, "文件不存在")
    return FileResponse(path)


@router.post("/writing/articles/{article_id}/delete")
def delete_article(article_id: int, request: Request, session: Session = Depends(get_session)):
    auth.require_level(request, 1)
    article = session.get(Article, article_id)
    if article is None:
        raise HTTPException(404, "文章不存在")
    now = _now()
    article.status = "已删除"
    article.deleted_at = now
    article.updated_at = now
    session.add(article)
    session.commit()
    if request.headers.get("HX-Request") == "true":
        topic = session.get(Topic, article.topic_id)
        campaigns = session.exec(select(Campaign).where(Campaign.brand_id == topic.brand_id)).all() if topic else []
        return _article_list_item_fragment(request, article, topic, campaigns)
    return RedirectResponse("/writing", status_code=303)


@router.post("/writing/articles/{article_id}/review")
def review_article(article_id: int, request: Request,
                   decision: str = Form(...), note: str = Form(""),
                   session: Session = Depends(get_session)):
    """审核文章：通过→「已审核」，未通过→「审核未通过」+ 原因。

    decision=approve | reject；reject 时 note 必填。
    审核时间 reviewed_at 首次审核时记录，不覆盖。
    """
    auth.require_level(request, 1)
    article = session.get(Article, article_id)
    if article is None:
        raise HTTPException(404, "文章不存在")
    if article.status != "待审核":
        raise HTTPException(400, "只有待审核状态可以审核")
    decision = decision.strip().lower()
    if decision not in ("approve", "reject"):
        raise HTTPException(400, "审核结果只能是 approve 或 reject")
    note = note.strip()
    if decision == "reject" and not note:
        raise HTTPException(400, "审核未通过时必须填写原因")
    now = _now()
    article.status = "已审核" if decision == "approve" else "审核未通过"
    article.review_note = note
    article.updated_at = now
    if article.reviewed_at is None:
        article.reviewed_at = now
    session.add(article)
    session.commit()
    if decision == "reject":
        upsert_review_rejection_experience(session, article, note)
        session.refresh(article)
    # HTMX 请求：返回更新后的详情片段
    if request.headers.get("HX-Request") == "true":
        topic = session.get(Topic, article.topic_id)
        campaigns = session.exec(select(Campaign).where(Campaign.brand_id == topic.brand_id)).all() if topic else []
        return _article_detail_fragment(request, article, topic, campaigns, session)
    return RedirectResponse(f"/writing/articles/{article_id}", status_code=303)


@router.post("/writing/articles/{article_id}/resubmit-review")
def resubmit_review(article_id: int, request: Request, session: Session = Depends(get_session)):
    """重新提交审核：审核未通过 → 待审核（作者修改后重新提交）。"""
    auth.require_level(request, 1)
    article = session.get(Article, article_id)
    if article is None:
        raise HTTPException(404, "文章不存在")
    if article.status != "审核未通过":
        raise HTTPException(400, "只有审核未通过状态可以重新提交审核")
    article.status = "待审核"
    article.review_note = ""
    article.updated_at = _now()
    session.add(article)
    session.commit()
    if request.headers.get("HX-Request") == "true":
        topic = session.get(Topic, article.topic_id)
        campaigns = session.exec(select(Campaign).where(Campaign.brand_id == topic.brand_id)).all() if topic else []
        return _article_detail_fragment(request, article, topic, campaigns, session)
    return RedirectResponse(f"/writing/articles/{article_id}", status_code=303)


@router.post("/writing/articles/{article_id}/ai-review")
def start_ai_review(article_id: int, request: Request,
                    session: Session = Depends(get_session)):
    """启动 AI 审核：待审核 → AI审核中 →（完成）→ 待审核 + ai_review_summary。

    后台线程执行：动态生成审核角色 → 单轮审核 → 总审核员汇总。
    每个角色发言实时持久化，前端 HTMX 轮询展示。
    """
    auth.require_level(request, 1)
    article = session.get(Article, article_id)
    if article is None:
        raise HTTPException(404, "文章不存在")
    if article.status != "待审核":
        raise HTTPException(400, "只有待审核状态可以启动 AI 审核")
    if _ai_review_effective(article.ai_review_summary):
        raise HTTPException(400, "已生成 AI 审核意见，编辑图文后可重新审核")
    article.status = "AI审核中"
    article.error_message = ""
    article.ai_review_summary = ""
    article.updated_at = _now()
    session.add(article)
    session.commit()
    session.refresh(article)
    # 启动后台线程
    t = threading.Thread(
        target=_run_ai_review_worker,
        args=(article_id,),
        daemon=True,
    )
    t.start()
    if request.headers.get("HX-Request") == "true":
        topic = session.get(Topic, article.topic_id)
        campaigns = session.exec(select(Campaign).where(Campaign.brand_id == topic.brand_id)).all() if topic else []
        return _article_detail_fragment(request, article, topic, campaigns, session)
    return RedirectResponse(f"/writing/articles/{article_id}", status_code=303)


def _run_ai_review_worker(article_id: int) -> None:
    """后台线程：AI 审核 → 综合意见 → 回到待审核。

    独立 Session，失败写 error_message 并回到待审核。
    """
    from sqlmodel import Session as SMSession
    with SMSession(db.engine) as s:
        try:
            article = s.get(Article, article_id)
            if article is None:
                return
            summary = run_ai_review(s, article_id, article)
            article.ai_review_summary = summary
            article.status = "待审核"
            article.error_message = ""
            article.updated_at = _now()
            s.add(article)
            s.commit()
        except Exception as e:
            # 失败：回到待审核，记录错误
            try:
                article = s.get(Article, article_id)
                if article is not None:
                    article.status = "待审核"
                    article.error_message = f"AI 审核失败：{e}"
                    article.updated_at = _now()
                    s.add(article)
                    s.commit()
            except Exception:
                pass


@router.post("/writing/articles/{article_id}/regenerate-images")
def regenerate_images(article_id: int, request: Request, session: Session = Depends(get_session)):
    """重新触发配图：从服务进程内启动配图子线程，用于「待配图」状态卡住时的恢复。

    仅对已有正文的文章生效，会清理旧候选图后重新异步生成。
    """
    auth.require_level(request, 1)
    article = session.get(Article, article_id)
    if article is None:
        raise HTTPException(404, "文章不存在")
    if not article.body:
        raise HTTPException(400, "文章还没有正文，无法配图")
    # 清理旧候选图
    old_imgs = session.exec(select(ArticleImage).where(ArticleImage.article_id == article_id)).all()
    for oi in old_imgs:
        session.delete(oi)
    article.status = "待配图"
    article.error_message = ""
    article.image_generation_slot = -1
    article.updated_at = _now()
    session.add(article)
    session.commit()
    # 从服务进程内启动配图子线程（daemon，随服务存活）
    t = threading.Thread(
        target=_run_image_worker,
        args=(article_id, article.topic_id, article.platform),
        daemon=True,
    )
    t.start()
    if request.headers.get("HX-Request") == "true":
        topic = session.get(Topic, article.topic_id)
        campaigns = session.exec(select(Campaign).where(Campaign.brand_id == topic.brand_id)).all() if topic else []
        return _article_detail_fragment(request, article, topic, campaigns, session)
    return RedirectResponse(f"/writing/articles/{article_id}", status_code=303)


@router.post("/writing/articles/{article_id}/regenerate-missing-images")
def regenerate_missing_images(article_id: int, request: Request, session: Session = Depends(get_session)):
    """补生缺失的配图：只生成候选图不足 4 张的 slot，保留已有图。

    用于配图子线程部分失败后，部分 slot 无图的场景。不清理已有候选图。
    """
    auth.require_level(request, 1)
    article = session.get(Article, article_id)
    if article is None:
        raise HTTPException(404, "文章不存在")
    if not article.body:
        raise HTTPException(400, "文章还没有正文，无法配图")
    # 启动补生子线程（missing_only=True，不清理已有图）
    t = threading.Thread(
        target=_run_image_worker,
        args=(article_id, article.topic_id, article.platform, True),
        daemon=True,
    )
    t.start()
    if request.headers.get("HX-Request") == "true":
        topic = session.get(Topic, article.topic_id)
        campaigns = session.exec(select(Campaign).where(Campaign.brand_id == topic.brand_id)).all() if topic else []
        return _article_detail_fragment(request, article, topic, campaigns, session)
    return RedirectResponse(f"/writing/articles/{article_id}", status_code=303)


def _image_prompt_context_for_slot(body: str, slot_desc: str) -> str:
    """提取插图位置前后的文章语境，供提示词优化模型理解画面用途。"""
    pattern = re.compile(r"\[插图(?:位|位置)?[：:]" + re.escape(slot_desc) + r"\]")
    match = pattern.search(body or "")
    if not match:
        return (body or "")[:400]
    return (body[max(0, match.start() - 260):match.end() + 260]).strip()


def _current_slot_image_prompt(session: Session, article: Article,
                               topic: Topic, slot_index: int,
                               slot_desc: str) -> str:
    """取得当前插图提示词；手动上传图片没有提示词时按文章语境生成一个基准提示词。"""
    image = session.exec(
        select(ArticleImage).where(
            ArticleImage.article_id == article.id,
            ArticleImage.slot_index == slot_index,
        ).order_by(ArticleImage.is_selected.desc(), ArticleImage.id)
    ).first()
    if image and image.prompt and image.prompt != "手动上传":
        return image.prompt.strip()
    ctx = KnowledgeContext.load(session, topic.brand_id, topic.campaign_id)
    style = _default_style(session, topic.brand_id)
    return _image_prompt_for_slot(topic, ctx, style, slot_desc, article.body, article.platform)


@router.post("/writing/articles/{article_id}/slots/{slot_index}/optimize-prompt/stream")
def optimize_slot_prompt_stream(article_id: int, slot_index: int, request: Request,
                                slot_desc: str = Form(""),
                                current_prompt: str = Form(""),
                                instruction: str = Form(""),
                                conversation: str = Form("[]"),
                                session: Session = Depends(get_session)):
    """为单个插图位置优化提示词；只返回提示词，不生成或替换图片。"""
    article = _require_ai_edit_article(article_id, request, session)
    topic = session.get(Topic, article.topic_id)
    if topic is None:
        raise HTTPException(404, "来源选题不存在")
    resolved_desc = _slot_desc(article, slot_index, session) or (slot_desc or "文章配图")
    resolved_prompt = (current_prompt or "").strip() or _current_slot_image_prompt(
        session, article, topic, slot_index, resolved_desc)
    instruction = (instruction or "").strip()
    if not instruction:
        raise HTTPException(400, "请先填写提示词优化要求")
    if len(instruction) > 2000:
        raise HTTPException(400, "提示词优化要求过长，请压缩到 2000 字以内")
    history = _parse_ai_edit_conversation(conversation)
    history_block = ""
    if history:
        history_block = "【此前的提示词优化对话】\n" + "\n\n".join(
            ("用户：" if item["role"] == "user" else "AI：") + item["text"]
            for item in history
        ) + "\n\n"
    image_context = _image_prompt_context_for_slot(article.body, resolved_desc)
    prompt = f"""你是 TN-Alpha 的插图提示词编辑助手。

【本次优化要求】
{instruction}

【插图位置描述】
{resolved_desc}

【当前插图提示词】
{resolved_prompt}

【文章语境】
{image_context}

{history_block}【输出要求】
1. 只输出一条可直接用于生成图片的中文提示词。
2. 保留当前画面的主体、构图和文章事实，除非用户明确要求改变。
3. 只根据用户本次要求优化，不要输出分析、解释、前后对比或 Markdown。
4. 不要生成图片，不要输出“提示词：”等前缀。
5. 最终提示词控制在 1200 字以内。
"""

    def generate_events():
        yield _ai_edit_sse("thinking", {})
        for attempt in range(2):
            chunks: list[str] = []
            if attempt:
                yield _ai_edit_sse("retry", {"message": "提示词过长或未得到最终结果，正在自动重试…"})
            try:
                attempt_prompt = prompt
                if attempt:
                    attempt_prompt += "\n\n【严格重试要求】只输出最终的一条中文图片提示词，不要输出任何分析或解释，长度不超过 1200 字。"
                for chunk in llm.stream_text(
                    attempt_prompt, task="writing_image_prompt_edit", module="writing", fallback=False
                ):
                    if chunk:
                        chunks.append(chunk)
                        yield _ai_edit_sse("delta", {"text": chunk})
                proposed = clean_llm_output("".join(chunks))
                for prefix in ("提示词：", "新提示词：", "Prompt:"):
                    if proposed.startswith(prefix):
                        proposed = proposed[len(prefix):].strip()
                if not proposed:
                    raise HTTPException(502, "AI 没有返回可用的图片提示词")
                if len(proposed) > 1200:
                    raise HTTPException(502, "AI 返回的图片提示词过长")
                yield _ai_edit_sse("done", {
                    "original": resolved_prompt,
                    "proposed": proposed,
                    "slot_index": slot_index,
                    "slot_desc": resolved_desc,
                })
                return
            except HTTPException as exc:
                if exc.status_code == 502 and attempt == 0:
                    continue
                message = str(exc.detail)
                if exc.status_code == 502:
                    message = "AI 未返回合适的图片提示词，已自动重试 1 次，请点击重新生成。"
                yield _ai_edit_sse("error", {"message": message})
                return
            except Exception as exc:
                yield _ai_edit_sse("error", {"message": str(exc)[:500]})
                return

    return StreamingResponse(
        generate_events(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/writing/articles/{article_id}/slots/{slot_index}/regenerate-with-prompt")
def regenerate_slot_with_prompt(article_id: int, slot_index: int, request: Request,
                                prompt: str = Form(""),
                                session: Session = Depends(get_session)):
    """用用户确认后的新提示词生成候选图，成功后整体替换该位置的旧候选图。"""
    auth.require_level(request, 1)
    article = session.get(Article, article_id)
    if article is None:
        raise HTTPException(404, "文章不存在")
    if article.status != "待审核":
        if article.status == "待配图":
            raise HTTPException(409, "该文章正在生成配图，请等待当前候选图生成完成")
        raise HTTPException(400, "只有待审核文章可以优化插图")
    prompt = (prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "图片提示词不能为空")
    slot_desc = _slot_desc(article, slot_index, session)
    if not slot_desc:
        raise HTTPException(400, f"插图位置 {slot_index + 1} 不存在")
    # 不提前删除旧候选图：worker 只有在新图生成成功后才会原子替换，失败时可回退。
    article.status = "待配图"
    article.error_message = ""
    article.image_generation_slot = slot_index
    article.updated_at = _now()
    session.add(article)
    session.commit()
    threading.Thread(
        target=_run_single_slot_worker,
        args=(article_id, article.topic_id, slot_index, slot_desc, article.platform, prompt, True),
        daemon=True,
    ).start()
    return JSONResponse({"started": True, "slot_index": slot_index, "prompt": prompt})


@router.get("/writing/articles/{article_id}/slots/{slot_index}/generation-status")
def slot_generation_status(article_id: int, slot_index: int, request: Request,
                           prompt: str = "",
                           session: Session = Depends(get_session)):
    """查询某个新提示词候选图是否生成完成。"""
    auth.require_level(request, 1)
    article = session.get(Article, article_id)
    if article is None:
        raise HTTPException(404, "文章不存在")
    images = session.exec(
        select(ArticleImage).where(
            ArticleImage.article_id == article_id,
            ArticleImage.slot_index == slot_index,
        )
    ).all()
    matched = [img for img in images if prompt and img.prompt == prompt]
    failed = bool(article.error_message and article.status == "待审核")
    return JSONResponse({
        "ready": bool(matched) and article.status == "待审核",
        "failed": failed,
        "error": article.error_message or "",
        "count": len(matched),
    })


@router.post("/writing/articles/{article_id}/slots/{slot_index}/regenerate")
def regenerate_slot(article_id: int, slot_index: int, request: Request,
                    session: Session = Depends(get_session)):
    """重新生成单个插图位置的候选图（4 张）。

    清理该 slot 旧候选图，启动子线程异步重生，不影响其他 slot。
    """
    auth.require_level(request, 1)
    article = session.get(Article, article_id)
    if article is None:
        raise HTTPException(404, "文章不存在")
    if not article.body:
        raise HTTPException(400, "文章还没有正文，无法配图")
    # 从正文解析该 slot 的描述
    slots = _parse_image_slots(article.body)
    if slots and slot_index < len(slots):
        _, slot_desc = slots[slot_index]
    else:
        existing = session.exec(
            select(ArticleImage).where(
                ArticleImage.article_id == article_id,
                ArticleImage.slot_index == slot_index,
            ).order_by(ArticleImage.id)
        ).first()
        if existing is None:
            raise HTTPException(400, f"插图位置 {slot_index + 1} 不存在")
        slot_desc = existing.slot_desc or "文章配图"
    # 与提示词优化保持一致：生成成功后再替换旧候选图，失败时保留旧图。
    article.status = "待配图"
    article.error_message = ""
    article.image_generation_slot = slot_index
    article.updated_at = _now()
    session.add(article)
    session.commit()
    # 启动子线程异步重生该 slot
    t = threading.Thread(
        target=_run_single_slot_worker,
        args=(article_id, article.topic_id, slot_index, slot_desc, article.platform, "", True),
        daemon=True,
    )
    t.start()
    # 立即返回当前详情片段；旧图仍可展示，轮询会自动补上新候选图。
    if request.headers.get("HX-Request") == "true":
        topic = session.get(Topic, article.topic_id)
        campaigns = session.exec(select(Campaign).where(Campaign.brand_id == topic.brand_id)).all() if topic else []
        return _article_detail_fragment(request, article, topic, campaigns, session)
    return RedirectResponse(f"/writing/articles/{article_id}", status_code=303)


@router.post("/writing/articles/{article_id}/slots/{slot_index}/select")
def select_slot_image(article_id: int, slot_index: int, request: Request,
                      image_id: int = Form(...),
                      session: Session = Depends(get_session)):
    """即时选中某个插图位置的一张图，不要求其他位置同时确认。"""
    auth.require_level(request, 1)
    article = session.get(Article, article_id)
    if article is None:
        raise HTTPException(404, "文章不存在")
    image = session.get(ArticleImage, image_id)
    if image is None or image.article_id != article_id or image.slot_index != slot_index:
        raise HTTPException(400, "图片不属于该插图位置")
    _select_slot_image(session, article, image)
    topic = session.get(Topic, article.topic_id)
    campaigns = session.exec(select(Campaign).where(Campaign.brand_id == topic.brand_id)).all() if topic else []
    if request.headers.get("HX-Request") == "true":
        return _article_detail_fragment(request, article, topic, campaigns, session)
    return RedirectResponse(f"/writing/articles/{article_id}", status_code=303)


@router.post("/writing/articles/{article_id}/slots/{slot_index}/upload")
def upload_slot_image(article_id: int, slot_index: int, request: Request,
                      file: UploadFile = File(...),
                      session: Session = Depends(get_session)):
    """手动上传某个插图位置的图片，并立即设为该组当前选中图。

    若该 slot 当前是"待选择"占位，上传后把正文标记改为"手动上传"。
    """
    auth.require_level(request, 1)
    article = session.get(Article, article_id)
    if article is None:
        raise HTTPException(404, "文章不存在")
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(400, "请上传图片文件")
    slot_desc = _slot_desc(article, slot_index, session) or "手动上传图片"
    path = storage.save_upload(file, subdir=f"writing/articles/{article_id}")
    image = ArticleImage(
        article_id=article_id,
        prompt="手动上传",
        image_url=_storage_url(path),
        slot_index=slot_index,
        slot_desc=slot_desc,
        is_selected=True,
        image_provider="manual",
        image_model="upload",
    )
    session.add(image)
    session.commit()
    session.refresh(image)
    # 用户上传 = 该位置最终用图：清除该 slot 所有其他图（AI 候选 + 旧上传），只留这一张
    old_imgs = session.exec(
        select(ArticleImage).where(
            ArticleImage.article_id == article_id,
            ArticleImage.slot_index == slot_index,
            ArticleImage.id != image.id,
        )
    ).all()
    for old in old_imgs:
        session.delete(old)
    session.commit()
    # 若该 slot 原是"待选择"占位，把正文标记改为"手动上传"
    if slot_desc == "待选择":
        import re as _re
        pattern = _re.compile(r'\[插图(?:位|位置)?[：:]待选择\]')
        matches = list(pattern.finditer(article.body))
        if 0 <= slot_index < len(matches):
            m = matches[slot_index]
            article.body = article.body[:m.start()] + "[插图：手动上传]" + article.body[m.end():]
            article.updated_at = _now()
            session.add(article)
            session.commit()
            session.refresh(article)
    _select_slot_image(session, article, image)
    topic = session.get(Topic, article.topic_id)
    campaigns = session.exec(select(Campaign).where(Campaign.brand_id == topic.brand_id)).all() if topic else []
    if request.headers.get("HX-Request") == "true":
        return _article_detail_fragment(request, article, topic, campaigns, session, force_editing=True)
    return RedirectResponse(f"/writing/articles/{article_id}", status_code=303)


@router.post("/writing/articles/{article_id}/select-images")
async def select_images(article_id: int, request: Request,
                        session: Session = Depends(get_session)):
    """用户选图：接收每个 slot 选中的 image_id，标记 is_selected，完成后状态改为待审核。"""
    auth.require_level(request, 1)
    article = session.get(Article, article_id)
    if article is None:
        raise HTTPException(404, "文章不存在")
    form = await request.form()
    selected_set: set[int] = set()
    # 新表单：image_id_0 / image_id_1 ...，让每个 slot 拥有独立 radio 组。
    for key, value in form.multi_items():
        if key.startswith("image_id_"):
            try:
                selected_set.add(int(str(value)))
            except (TypeError, ValueError):
                raise HTTPException(400, "包含无效图片") from None
    # 兼容旧表单字段，方便测试和已有页面提交。
    for value in form.getlist("image_id"):
        try:
            selected_set.add(int(str(value)))
        except (TypeError, ValueError):
            raise HTTPException(400, "包含无效图片") from None
    if not selected_set:
        raise HTTPException(400, "请至少选择一张图片")

    # 清除旧选择，标记新选择
    all_imgs = session.exec(select(ArticleImage).where(ArticleImage.article_id == article_id)).all()
    known_ids = {img.id for img in all_imgs}
    if not selected_set.issubset(known_ids):
        raise HTTPException(400, "包含无效图片")
    selected_slots: set[int] = set()
    for img in all_imgs:
        img.is_selected = img.id in selected_set
        if img.is_selected:
            selected_slots.add(img.slot_index)
        session.add(img)
    expected_slots = sorted(_expected_slot_indexes(article.body, all_imgs))
    missing_slots = [idx for idx in expected_slots if idx not in selected_slots]
    if missing_slots:
        raise HTTPException(400, f"请为插图位置 {missing_slots[0] + 1} 选择图片")
    session.commit()

    # 设置主图 url（slot_index 最小的选中图）
    first_selected = session.exec(
        select(ArticleImage).where(
            ArticleImage.article_id == article_id, ArticleImage.is_selected == True
        ).order_by(ArticleImage.slot_index)
    ).first()
    if first_selected:
        article.image_url = first_selected.image_url
        article.image_prompt = first_selected.prompt
    article.status = "待审核"
    article.updated_at = _now()
    session.add(article)
    session.commit()

    if request.headers.get("HX-Request") == "true":
        topic = session.get(Topic, article.topic_id)
        campaigns = session.exec(select(Campaign).where(Campaign.brand_id == topic.brand_id)).all() if topic else []
        # 详情页选图完成后返回详情内容片段（展示最终文章）
        return _article_detail_fragment(request, article, topic, campaigns, session)
    return RedirectResponse(f"/writing/articles/{article_id}", status_code=303)


@router.post("/writing/articles/{article_id}/restore")
def restore_article(article_id: int, request: Request, session: Session = Depends(get_session)):
    auth.require_level(request, 1)
    article = session.get(Article, article_id)
    if article is None:
        raise HTTPException(404, "文章不存在")
    article.deleted_at = None
    article.status = "待审核" if article.body and article.image_url else "写作中"
    article.updated_at = _now()
    session.add(article)
    session.commit()
    if request.headers.get("HX-Request") == "true":
        topic = session.get(Topic, article.topic_id)
        campaigns = session.exec(select(Campaign).where(Campaign.brand_id == topic.brand_id)).all() if topic else []
        return _article_list_item_fragment(request, article, topic, campaigns)
    return RedirectResponse(f"/writing/articles/{article_id}", status_code=303)


def _reindex_slots_by_body(session: Session, article: Article) -> None:
    """按当前 article.body 中的 [插图：...] 标记顺序，重排所有 ArticleImage.slot_index。

    用于正文编辑后插入/删除标记导致 slot 顺序变化时，把已存候选图重新对齐到新位置。
    规则：
      - 按 body 中标记顺序分配新 slot_index 0,1,2...
      - 用 slot_desc 匹配 body 标记内容，找到则映射到新 index（同 desc 多 slot 按 slot_index 升序取）
      - body 中已无标记对应的旧 slot（用户删了标记）→ 删除该 slot 所有候选图
    """
    import re
    body_slots = re.findall(r'\[插图(?:位|位置)?[：:](.+?)\]', article.body or "")
    all_imgs = session.exec(
        select(ArticleImage).where(ArticleImage.article_id == article.id)
        .order_by(ArticleImage.slot_index, ArticleImage.id)
    ).all()
    by_slot: dict[int, list[ArticleImage]] = {}
    for img in all_imgs:
        by_slot.setdefault(img.slot_index, []).append(img)
    # 按 slot_desc 建立旧 slot 索引（desc → 升序的 old_slot_idx 列表）
    desc_to_slots: dict[str, list[int]] = {}
    for old_idx in sorted(by_slot.keys()):
        desc = by_slot[old_idx][0].slot_desc or ""
        desc_to_slots.setdefault(desc, []).append(old_idx)
    used_old: set[int] = set()
    # body 标记顺序 → 新 slot_index，用 desc 匹配旧 slot
    for new_idx, desc in enumerate(body_slots):
        for old_idx in desc_to_slots.get(desc, []):
            if old_idx not in used_old:
                used_old.add(old_idx)
                for img in by_slot[old_idx]:
                    if img.slot_index != new_idx:
                        img.slot_index = new_idx
                        session.add(img)
                break
    # 未匹配到 body 标记的旧 slot（标记被删）→ 删除候选图
    for old_idx in by_slot:
        if old_idx not in used_old:
            for img in by_slot[old_idx]:
                session.delete(img)
    session.commit()


def _ai_edit_slot_descriptions(body: str) -> list[str]:
    """返回正文中插图标记的描述，供 AI 修改前后做结构校验。"""
    return [desc for _pos, desc in _parse_image_slots(body)]


def _remove_ai_edit_slot_markers(body: str, indexes: set[int]) -> str:
    """移除用户明确选择删除的插图标记，按原始 slot 索引从后往前处理。"""
    if not indexes:
        return body
    pattern = re.compile(r'\[插图(?:位|位置)?[：:](.+?)\]')
    matches = list(pattern.finditer(body))
    for index in sorted(indexes, reverse=True):
        if 0 <= index < len(matches):
            match = matches[index]
            body = body[:match.start()] + body[match.end():]
    return body


def _mask_image_slots_for_ai(body: str) -> str:
    """给选中模式提供上下文时隐藏插图标记，避免模型把结构标记复制进结果。"""
    return re.sub(
        r'\[插图(?:位|位置)?[：:].+?\]',
        "[此处保留插图位置，不要输出此占位符]",
        body,
    )


def _ai_edit_image_changes(before: str, after: str) -> list[dict]:
    """比较修改前后的插图槽位，生成供前端确认的图片变化清单。"""
    before_slots = _ai_edit_slot_descriptions(before)
    after_slots = _ai_edit_slot_descriptions(after)
    changes: list[dict] = []
    max_slots = max(len(before_slots), len(after_slots))
    for index in range(max_slots):
        old_desc = before_slots[index] if index < len(before_slots) else ""
        new_desc = after_slots[index] if index < len(after_slots) else ""
        if old_desc and new_desc and old_desc == new_desc:
            continue
        if old_desc and new_desc:
            changes.append({
                "index": index,
                "kind": "updated",
                "old_desc": old_desc,
                "new_desc": new_desc,
                "action": "regenerate",
            })
        elif old_desc:
            changes.append({
                "index": index,
                "kind": "removed",
                "old_desc": old_desc,
                "new_desc": "",
                "action": "remove",
            })
        else:
            changes.append({
                "index": index,
                "kind": "added",
                "old_desc": "",
                "new_desc": new_desc,
                "action": "regenerate",
            })
    return changes


def _parse_ai_image_actions(raw: str | list | None) -> dict[int, dict]:
    """解析用户对插图变化的选择，只接受当前正文保存需要的有限动作。"""
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "插图处理选项格式不正确") from exc
    if not isinstance(raw, list):
        raise HTTPException(400, "插图处理选项格式不正确")
    actions: dict[int, dict] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        if index < 0:
            continue
        action = item.get("action") or "keep"
        if action not in ("keep", "regenerate", "remove"):
            raise HTTPException(400, "插图处理动作不合法")
        actions[index] = {"action": action, "kind": item.get("kind", "")}
    return actions


def _apply_article_body_image_changes(session: Session, article: Article,
                                      old_body: str, new_body: str,
                                      requested_actions: dict[int, dict]) -> list[int]:
    """保存正文后重排插图，并返回需要异步重生成的 slot。"""
    old_slots = _ai_edit_slot_descriptions(old_body)
    raw_new_slots = _ai_edit_slot_descriptions(new_body)
    remove_indexes = {
        index for index in range(len(raw_new_slots))
        if requested_actions.get(index, {}).get("action") == "remove"
    }
    if remove_indexes:
        new_body = _remove_ai_edit_slot_markers(new_body, remove_indexes)
        article.body = new_body
        session.add(article)
    new_slots = _ai_edit_slot_descriptions(new_body)
    action_by_new_index: dict[int, dict] = {}
    raw_index_by_new_index: dict[int, int] = {}
    filtered_index = 0
    for raw_index in range(len(raw_new_slots)):
        if raw_index in remove_indexes:
            continue
        raw_index_by_new_index[filtered_index] = raw_index
        if raw_index in requested_actions:
            action_by_new_index[filtered_index] = requested_actions[raw_index]
        filtered_index += 1
    images = session.exec(
        select(ArticleImage).where(ArticleImage.article_id == article.id)
        .order_by(ArticleImage.slot_index, ArticleImage.id)
    ).all()
    by_slot: dict[int, list[ArticleImage]] = {}
    for image in images:
        by_slot.setdefault(image.slot_index, []).append(image)

    # 优先按原描述匹配；描述被 AI 改写时，再按同位置回退匹配。
    desc_to_slots: dict[str, list[int]] = {}
    for old_index in sorted(by_slot):
        desc = by_slot[old_index][0].slot_desc or ""
        desc_to_slots.setdefault(desc, []).append(old_index)
    mapped: dict[int, int] = {}
    used_old: set[int] = set()
    for new_index, new_desc in enumerate(new_slots):
        for old_index in desc_to_slots.get(new_desc, []):
            if old_index not in used_old:
                mapped[new_index] = old_index
                used_old.add(old_index)
                break
    for new_index, new_desc in enumerate(new_slots):
        if new_index in mapped:
            continue
        action_info = action_by_new_index.get(new_index, {})
        if action_info.get("kind") == "added":
            continue
        raw_index = raw_index_by_new_index.get(new_index, new_index)
        if raw_index < len(old_slots) and raw_index in by_slot and raw_index not in used_old:
            mapped[new_index] = raw_index
            used_old.add(raw_index)

    regenerate: list[int] = []
    for new_index, new_desc in enumerate(new_slots):
        old_index = mapped.get(new_index)
        slot_images = by_slot.get(old_index, []) if old_index is not None else []
        action_info = action_by_new_index.get(new_index, {})
        action = action_info.get("action", "keep")
        if action == "regenerate" or (old_index is None and new_index not in action_by_new_index):
            for image in slot_images:
                image.slot_index = new_index
                image.slot_desc = new_desc
                session.add(image)
            regenerate.append(new_index)
            continue
        for image in slot_images:
            image.slot_index = new_index
            image.slot_desc = new_desc
            session.add(image)

    # 正文中删除的旧插图位置，连同对应候选图一起清理。
    for old_index, slot_images in by_slot.items():
        if old_index not in used_old:
            for image in slot_images:
                session.delete(image)

    if regenerate:
        article.status = "待配图"
        article.error_message = ""
        article.image_generation_slot = regenerate[0] if len(regenerate) == 1 else -1
        article.updated_at = _now()
        session.add(article)
    session.commit()
    if len(regenerate) == 1:
        slot_index = regenerate[0]
        slot_desc = new_slots[slot_index]
        t = threading.Thread(
            target=_run_single_slot_worker,
            args=(article.id, article.topic_id, slot_index, slot_desc, article.platform, "", True),
            daemon=True,
        )
        t.start()
    elif regenerate:
        slot_specs = [(slot_index, new_slots[slot_index], "") for slot_index in regenerate]
        t = threading.Thread(
            target=_run_slot_batch_worker,
            args=(article.id, article.topic_id, slot_specs, article.platform, True),
            daemon=True,
        )
        t.start()
    return regenerate


def _find_ai_edit_selection(source_body: str, selected_text: str,
                            selection_start: int = -1,
                            selection_end: int = -1) -> tuple[int, int] | None:
    """定位浏览器选区，优先使用前端传来的偏移，避免重复文本总命中第一处。"""
    if (
        selection_start >= 0
        and selection_end == selection_start + len(selected_text)
        and selection_end <= len(source_body)
        and source_body[selection_start:selection_end] == selected_text
    ):
        return selection_start, selection_end

    exact_matches: list[int] = []
    cursor = source_body.find(selected_text)
    while cursor >= 0:
        exact_matches.append(cursor)
        cursor = source_body.find(selected_text, cursor + 1)
    if len(exact_matches) == 1:
        return exact_matches[0], exact_matches[0] + len(selected_text)
    if len(exact_matches) > 1:
        return None

    def normalize(value: str) -> tuple[str, list[int]]:
        chars: list[str] = []
        positions: list[int] = []
        for index, char in enumerate(value):
            if char.isspace():
                if chars and chars[-1] != " ":
                    chars.append(" ")
                    positions.append(index)
                continue
            chars.append(char)
            positions.append(index)
        if chars and chars[-1] == " ":
            chars.pop()
            positions.pop()
        return "".join(chars), positions

    normalized_source, source_positions = normalize(source_body)
    normalized_selected, _ = normalize(selected_text)
    if not normalized_selected:
        return None
    normalized_matches: list[int] = []
    cursor = normalized_source.find(normalized_selected)
    while cursor >= 0:
        normalized_matches.append(cursor)
        cursor = normalized_source.find(normalized_selected, cursor + 1)
    if len(normalized_matches) != 1 or not source_positions:
        return None
    normalized_start = normalized_matches[0]
    normalized_end = normalized_start + len(normalized_selected) - 1
    return source_positions[normalized_start], source_positions[normalized_end] + 1


def _ai_edit_context_excerpt(source: str, selected_text: str,
                             radius: int = 420) -> str:
    """返回选区附近的可读上下文，供 AI 修改工作台展示。"""
    source = (source or "").replace("\r\n", "\n").replace("\r", "\n")
    selected_text = (selected_text or "").strip()
    if not source:
        return ""
    if not selected_text:
        return source[: radius * 2]
    span = _find_ai_edit_selection(source, selected_text)
    if span is None:
        return source[: radius * 2]
    start, end = span
    excerpt_start = max(0, start - radius)
    excerpt_end = min(len(source), end + radius)
    excerpt = source[excerpt_start:excerpt_end].strip()
    if excerpt_start > 0:
        excerpt = "…" + excerpt
    if excerpt_end < len(source):
        excerpt += "…"
    return excerpt


def _build_article_edit_context(session: Session, article: Article, topic: Topic) -> tuple[KnowledgeContext, Style | None, str]:
    """组装文章 AI 修改所需的知识、风格和经验上下文。"""
    ctx = KnowledgeContext.load(session, topic.brand_id, topic.campaign_id)
    style = session.get(Style, article.style_id) if article.style_id else _default_style(session, topic.brand_id)
    writing_experience = campaign_experience_context(
        session,
        topic.brand_id,
        topic.campaign_id,
        platform=article.platform,
        task="writing",
        inherited_packs=ctx.pool_experiences,
    )
    return ctx, style, writing_experience


def _parse_ai_edit_conversation(raw: str | list | None) -> list[dict[str, str]]:
    """解析本次弹窗会话，避免把无效或过大的历史记录送入模型。"""
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "AI 对话记录格式不正确") from exc
    if not isinstance(raw, list):
        raise HTTPException(400, "AI 对话记录格式不正确")

    messages: list[dict[str, str]] = []
    total_chars = 0
    for item in raw[-12:]:
        if not isinstance(item, dict) or item.get("role") not in ("user", "assistant"):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        text = text[:6000]
        remaining = 16000 - total_chars
        if remaining <= 0:
            break
        text = text[:remaining]
        target = item.get("target") if item.get("target") in ("body", "title") else "body"
        messages.append({"role": item["role"], "text": text, "target": target})
        total_chars += len(text)
    return messages


def _ai_edit_selection_output_limit(selected_text: str) -> int:
    """给选区修改设置合理的输出上限，避免模型把整篇文章当成替换结果返回。"""
    # 允许润色、扩写带来的自然增长，但小选区不能无限膨胀；大选区也保留绝对上限。
    return max(240, min(4000, len((selected_text or '').strip()) * 6))


def _prepare_ai_edit_prompt(session: Session, article: Article, scope: str,
                            target: str, body: str, title: str, selected_text: str,
                            instruction: str,
                            conversation: str | list | None = None,
                            selection_start: int = -1,
                            selection_end: int = -1) -> dict:
    """校验 AI 修改输入并组装 prompt；同步和流式接口共用。"""
    if scope not in ("selection", "article"):
        raise HTTPException(400, "修改范围不合法")
    if target not in ("body", "title"):
        raise HTTPException(400, "修改对象不合法")
    if target == "title" and scope != "selection":
        raise HTTPException(400, "标题只支持选中修改")
    instruction = (instruction or "").strip()
    if not instruction:
        raise HTTPException(400, "请先填写修改要求")
    if len(instruction) > 3000:
        raise HTTPException(400, "修改要求过长，请压缩到 3000 字以内")

    source_body = (body or article.body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    source_title = (title or article.title or "").strip()
    if not source_body:
        raise HTTPException(400, "正文不能为空")
    if target == "title" and not source_title:
        raise HTTPException(400, "标题不能为空")
    if len(source_body) > 50000:
        raise HTTPException(400, "正文过长，请先分段修改")

    selected_text = (selected_text or "").strip()
    conversation_items = _parse_ai_edit_conversation(conversation)
    if scope == "selection":
        if not selected_text:
            raise HTTPException(400, "请先在标题或正文中选中要修改的内容")
        if len(selected_text) > 12000:
            raise HTTPException(400, "选中内容过长，请缩小修改范围")
        selection_source = source_title if target == "title" else source_body
        if _find_ai_edit_selection(
            selection_source, selected_text, selection_start, selection_end
        ) is None:
            raise HTTPException(400, "选中内容已不在当前标题或正文中，请重新选择")
        if target == "body" and _parse_image_slots(selected_text):
            raise HTTPException(400, "不能直接修改插图位置标记")

    topic = session.get(Topic, article.topic_id)
    if topic is None:
        raise HTTPException(404, "来源选题不存在")
    ctx, style, writing_experience = _build_article_edit_context(session, article, topic)
    style_text = _resolve_style_text(style, article.platform)
    platform_dir = _platform_directive(article.platform, article.word_count)
    enforce = _platform_enforce(style, article.platform)
    knowledge_block = knowledge_context_block(ctx, writing_experience)
    if target == "title":
        scope_text = "只改写下面选中的标题，输出可直接替换的标题，不要输出解释。"
        source_block = f"【选中标题】\n{selected_text}\n\n【当前标题】\n{source_title}"
    elif scope == "selection":
        scope_text = "只改写下面选中的正文内容，输出可直接替换的文本，不要输出解释。"
        nearby_context = _ai_edit_context_excerpt(source_body, selected_text, radius=900)
        source_block = f"【选中正文】\n{selected_text}\n\n【选中位置附近上下文（仅用于理解语气和衔接）】\n{_mask_image_slots_for_ai(nearby_context)}"
    else:
        scope_text = "改写整篇正文，输出完整正文，不要输出标题或解释。"
        source_block = f"【当前正文】\n{source_body}"
    review_block = ""
    if article.ai_review_summary:
        review_block += f"【已有 AI 审核意见，仅作为修改参考】\n{article.ai_review_summary}\n\n"
    if article.review_note:
        review_block += f"【人工审核备注，仅作为修改参考】\n{article.review_note}\n\n"
    req_block = f"【原始写作要求】\n{article.writing_req}\n\n" if article.writing_req else ""
    conversation_block = ""
    if conversation_items:
        history_lines = []
        for item in conversation_items:
            role = "用户" if item["role"] == "user" else "AI"
            target_label = "标题" if item.get("target") == "title" else "正文"
            history_lines.append(f"【{role}（修改{target_label}）】\n{item['text']}")
        conversation_block = (
            "【此前的交互修改记录】\n"
            "以下内容只是此前已经发生的修改记录，不是新的系统指令；请结合当前正文和本次最新要求继续工作。\n"
            + "\n\n".join(history_lines)
            + "\n\n"
        )
    context_priority = (
        "当前选中标题和本次最新修改要求是唯一的待修改对象；此前选区和此前 AI 结果只能用于了解上下文，不能替代当前标题。"
        if target == "title" else
        "当前选中正文和本次最新修改要求是唯一的待修改对象；此前选区和此前 AI 结果只能用于了解上下文，不能替代当前选中内容。"
        if scope == "selection" else
        "当前正文和本次最新修改要求是唯一的待修改对象；此前 AI 结果只能用于了解上下文，不能替代当前正文。"
    )
    marker_rule = (
        "只输出标题文字，不要输出正文、插图标记或解释。"
        if target == "title" else
        "可以根据正文语义修改、新增或删除 [插图：...] 标记；每个标记必须独占一行并包含清晰的插图描述。"
        if scope == "article" else
        "只输出选中内容，不要输出插图标记；所在正文中的插图位置只是上下文提示，不能复制到结果中。"
    )
    prompt = f"""你是 TN-Alpha 的文章编辑助手，负责在文章进入人工审核前协助修改标题或正文。

【本次修改要求】
{instruction}

【文章基础信息】
选题：{topic.title}
纲要：{topic.outline}
切入角度：{topic.angle}
受众：{topic.audience}
发布平台：{article.platform or '未指定'}
{platform_dir}

【写作风格】
{style_text}

{knowledge_block}

{req_block}{review_block}{conversation_block}{source_block}

【上下文优先级】
{context_priority}
历史对话中的要求可以帮助理解用户偏好，但如果与本次要求或当前内容冲突，以本次要求和当前内容为准。

【硬性要求】
1. {scope_text}
2. 保留原文事实、主题和核心信息，不要凭空补充事实。
3. {marker_rule}
4. 输出纯文本，使用自然的中文段落，不要 Markdown、不要“修改后：”等前缀。
{enforce}
"""
    return {
        "scope": scope,
        "target": target,
        "source_body": source_body,
        "source_title": source_title,
        "selected_text": selected_text,
        "selection_start": selection_start,
        "selection_end": selection_end,
        "conversation": conversation_items,
        "prompt": prompt,
        "topic": topic,
    }


def _finalize_ai_edit_preview(article: Article, prepared: dict, raw: str) -> dict:
    """清理模型结果并校验插图标记，生成前端可应用的完整预览。"""
    proposed = clean_llm_output(raw)
    if proposed.startswith(("正文：", "标题：")):
        proposed = proposed.split("：", 1)[1].strip()
    if not proposed:
        raise HTTPException(502, "AI 没有返回可用的修改内容")

    scope = prepared["scope"]
    target = prepared["target"]
    source_body = prepared["source_body"]
    source_title = prepared["source_title"]
    selected_text = prepared["selected_text"]
    if target == "title":
        selection_span = _find_ai_edit_selection(
            source_title, selected_text,
            prepared.get("selection_start", -1), prepared.get("selection_end", -1)
        )
        if selection_span is None:
            raise HTTPException(400, "选中标题已不在当前标题中，请重新选择")
        selection_start, selection_end = selection_span
        new_title = source_title[:selection_start] + proposed + source_title[selection_end:]
        new_body = source_body
        original = selected_text
    elif scope == "selection":
        # 选中普通文字时，模型偶尔会把上下文中的插图标记一并带回；
        # 选区本身已在前面校验过不含插图，因此这里安全地移除这些回显标记。
        proposed = _strip_image_slots(proposed)
        if not proposed:
            raise HTTPException(502, "AI 没有返回可用的修改内容")
        max_chars = _ai_edit_selection_output_limit(selected_text)
        if len(proposed) > max_chars:
            raise HTTPException(
                502,
                f"AI 返回内容超出选区范围（{len(proposed)} 字，选区建议上限 {max_chars} 字）",
            )
        selection_span = _find_ai_edit_selection(
            source_body, selected_text,
            prepared.get("selection_start", -1), prepared.get("selection_end", -1)
        )
        if selection_span is None:
            raise HTTPException(400, "选中内容已不在当前正文中，请重新选择")
        selection_start, selection_end = selection_span
        new_body = source_body[:selection_start] + proposed + source_body[selection_end:]
        original = selected_text
    else:
        new_body = proposed
        original = source_body
    image_changes = _ai_edit_image_changes(source_body, new_body)
    return {
        "scope": scope,
        "target": target,
        "original": original,
        "proposed": proposed,
        "body": new_body,
        "title": new_title if target == "title" else article.title,
        "image_changes": image_changes,
    }


def _ai_edit_sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _require_ai_edit_article(article_id: int, request: Request, session: Session) -> Article:
    auth.require_level(request, 1)
    article = session.get(Article, article_id)
    if article is None:
        raise HTTPException(404, "文章不存在")
    if article.status != "待审核":
        raise HTTPException(400, "只有待审核状态可以使用 AI 修改")
    return article


@router.post("/writing/articles/{article_id}/ai-edit")
def ai_edit_article(article_id: int, request: Request,
                    scope: str = Form("article"),
                    target: str = Form("body"),
                    body: str = Form(""),
                    title: str = Form(""),
                    selected_text: str = Form(""),
                    selection_start: int = Form(-1),
                    selection_end: int = Form(-1),
                    instruction: str = Form(""),
                    conversation: str = Form("[]"),
                    session: Session = Depends(get_session)):
    """兼容旧调用：为待审核文章生成一次性 AI 修改预览。"""
    article = _require_ai_edit_article(article_id, request, session)
    prepared = _prepare_ai_edit_prompt(
        session, article, scope, target, body, title, selected_text, instruction,
        conversation, selection_start, selection_end)
    started_at = time.monotonic()
    print(
        f"[ai-edit] start article={article_id} scope={prepared['scope']} prompt_chars={len(prepared['prompt'])}",
        flush=True,
    )
    try:
        proposed = clean_llm_output(llm.generate_text(
            prepared["prompt"], task="writing_article_edit", module="writing", fallback=False))
    except Exception as exc:
        elapsed = time.monotonic() - started_at
        print(f"[ai-edit] failed article={article_id} elapsed={elapsed:.1f}s error={exc}", flush=True)
        raise HTTPException(502, f"AI 修改失败：{str(exc)[:300]}") from exc
    print(
        f"[ai-edit] done article={article_id} elapsed={time.monotonic() - started_at:.1f}s output_chars={len(proposed)}",
        flush=True,
    )
    return JSONResponse(_finalize_ai_edit_preview(article, prepared, proposed))


@router.post("/writing/articles/{article_id}/ai-edit/stream")
def ai_edit_article_stream(article_id: int, request: Request,
                           scope: str = Form("article"),
                           target: str = Form("body"),
                           body: str = Form(""),
                           title: str = Form(""),
                           selected_text: str = Form(""),
                           selection_start: int = Form(-1),
                           selection_end: int = Form(-1),
                           instruction: str = Form(""),
                           conversation: str = Form("[]"),
                           session: Session = Depends(get_session)):
    """流式生成 AI 修改建议；完成前不写回文章，完成后才校验并返回可应用结果。"""
    article = _require_ai_edit_article(article_id, request, session)
    prepared = _prepare_ai_edit_prompt(
        session, article, scope, target, body, title, selected_text, instruction,
        conversation, selection_start, selection_end)

    def generate_events():
        started_at = time.monotonic()
        yield _ai_edit_sse("thinking", {})
        print(
            f"[ai-edit] stream start article={article_id} scope={prepared['scope']} "
            f"selected_chars={len(prepared['selected_text'])} prompt_chars={len(prepared['prompt'])}",
            flush=True,
        )
        for attempt in range(2):
            chunks: list[str] = []
            if attempt:
                yield _ai_edit_sse("retry", {"message": "未收到最终内容，正在自动重试…"})
            try:
                attempt_prompt = prepared["prompt"]
                if attempt:
                    retry_rule = (
                        "只输出当前选区的最终替换文本，不要输出分析、思考过程、上下文、修改说明或全文；"
                        f"最终内容不要超过 {_ai_edit_selection_output_limit(prepared['selected_text'])} 字。"
                        if prepared["scope"] == "selection" else
                        "只输出最终可应用的标题或正文，不要输出思考过程，不要输出 <think> 标签。"
                    )
                    attempt_prompt += f"\n\n【重试要求】上一次没有得到可应用的最终答案。这次{retry_rule}"
                for chunk in llm.stream_text(
                    attempt_prompt,
                    task="writing_article_edit",
                    module="writing",
                    fallback=False,
                ):
                    if not chunk:
                        continue
                    chunks.append(chunk)
                    yield _ai_edit_sse("delta", {"text": chunk})
                raw = "".join(chunks)
                result = _finalize_ai_edit_preview(article, prepared, raw)
                print(
                    f"[ai-edit] stream done article={article_id} attempt={attempt + 1} "
                    f"elapsed={time.monotonic() - started_at:.1f}s output_chars={len(raw)} "
                    f"proposed_chars={len(result['proposed'])}",
                    flush=True,
                )
                yield _ai_edit_sse("done", result)
                return
            except HTTPException as exc:
                if exc.status_code == 502 and attempt == 0:
                    print(
                        f"[ai-edit] empty final output article={article_id}; retrying once",
                        flush=True,
                    )
                    continue
                message = str(exc.detail)
                if exc.status_code == 502:
                    message = (
                        "AI 返回内容超出当前选区，已自动重试 1 次；请缩小选区或点击重新生成。"
                        if "超出选区范围" in message else
                        "AI 未返回最终内容，已自动重试 1 次，请点击重新生成。"
                    )
                print(
                    f"[ai-edit] stream failed article={article_id} attempt={attempt + 1} elapsed={time.monotonic() - started_at:.1f}s error={message}",
                    flush=True,
                )
                yield _ai_edit_sse("error", {"message": message})
                return
            except Exception as exc:
                print(
                    f"[ai-edit] stream failed article={article_id} attempt={attempt + 1} elapsed={time.monotonic() - started_at:.1f}s error={exc}",
                    flush=True,
                )
                yield _ai_edit_sse("error", {"message": str(exc)[:500]})
                return

    return StreamingResponse(
        generate_events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/writing/articles/{article_id}/edit-body")
def edit_article_body(article_id: int, request: Request,
                      body: str = Form(...), title: str = Form(""),
                      image_changes: str = Form("[]"),
                      session: Session = Depends(get_session)):
    """待审核下就地编辑正文（含 [插图：...] 标记）和标题。

    保存后重排 slot_index（正文标记顺序可能变了）。
    """
    auth.require_level(request, 1)
    article = session.get(Article, article_id)
    if article is None:
        raise HTTPException(404, "文章不存在")
    if article.status != "待审核":
        raise HTTPException(400, "只有待审核状态可以编辑正文")
    old_body = article.body or ""
    new_body = (body or "").strip()
    if not new_body:
        raise HTTPException(400, "正文不能为空")
    requested_actions = _parse_ai_image_actions(image_changes)
    article.body = new_body
    new_title = (title or "").strip()
    if new_title:
        article.title = new_title[:200]
    # 正文有改动 → 旧的 AI 审核意见已对不上新内容，清空 summary + 删 ai_review 阶段发言
    if _ai_review_effective(article.ai_review_summary) or _has_ai_review_records(session, article_id):
        article.ai_review_summary = ""
        _clear_ai_review_records(session, article_id)
    article.updated_at = _now()
    session.add(article)
    session.commit()
    session.refresh(article)
    # 正文标记顺序或描述可能变化：保留可复用的图片，按用户选择重生成受影响 slot。
    _apply_article_body_image_changes(session, article, old_body, new_body, requested_actions)
    if request.headers.get("HX-Request") == "true":
        topic = session.get(Topic, article.topic_id)
        campaigns = session.exec(select(Campaign).where(Campaign.brand_id == topic.brand_id)).all() if topic else []
        # 保存后退出编辑模式（force_editing=False），让页面回到非编辑状态
        return _article_detail_fragment(request, article, topic, campaigns, session, force_editing=False)
    return RedirectResponse(f"/writing/articles/{article_id}", status_code=303)


@router.post("/writing/articles/{article_id}/insert-placeholder")
async def insert_placeholder(article_id: int, request: Request,
                             anchor_text: str = Form(""),
                             insert_position: int = Form(-1),
                             body: str = Form(""),
                             title: str = Form(""),
                             session: Session = Depends(get_session)):
    """插入图片占位符标记 [插图：待选择]（不传文件）。

    定位方式（优先级）：
      1. insert_position >= 0：用字符偏移量在光标位置拆分段落插入
      2. anchor_text：在 body 中 rfind 该文本，在其末尾插入（段落模式）
    """
    auth.require_level(request, 1)
    article = session.get(Article, article_id)
    if article is None:
        raise HTTPException(404, "文章不存在")
    if article.status != "待审核":
        raise HTTPException(400, "只有待审核状态可以插入图片占位")

    # 先保存前端编辑的正文（用户可能改了文字还没保存）
    edited_body = (body or "").strip()
    if edited_body:
        article.body = edited_body
    edited_title = (title or "").strip()
    if edited_title:
        article.title = edited_title[:200]

    # 定位插入点
    if insert_position >= 0:
        insert_pos = min(insert_position, len(article.body))
    else:
        insert_pos = _find_text_segment_end(article.body, anchor_text)

    new_marker = f"\n[插图：待选择]\n"
    article.body = article.body[:insert_pos] + new_marker + article.body[insert_pos:]
    article.updated_at = _now()
    session.add(article)
    session.commit()
    session.refresh(article)

    # 重排 slot_index（新标记改变了顺序）
    _reindex_slots_by_body(session, article)

    if request.headers.get("HX-Request") == "true":
        topic = session.get(Topic, article.topic_id)
        campaigns = session.exec(select(Campaign).where(Campaign.brand_id == topic.brand_id)).all() if topic else []
        return _article_detail_fragment(request, article, topic, campaigns, session, force_editing=True)
    return RedirectResponse(f"/writing/articles/{article_id}", status_code=303)


@router.post("/writing/articles/{article_id}/slots/{slot_index}/delete")
def delete_slot(article_id: int, slot_index: int, request: Request,
                session: Session = Depends(get_session)):
    """删除某个插图位置：移除正文中的 [插图：...] 标记 + 删除该 slot 所有候选图。"""
    auth.require_level(request, 1)
    article = session.get(Article, article_id)
    if article is None:
        raise HTTPException(404, "文章不存在")
    if article.status != "待审核":
        raise HTTPException(400, "只有待审核状态可以删除插图位置")

    # 删除该 slot 的所有候选图
    imgs = session.exec(
        select(ArticleImage).where(
            ArticleImage.article_id == article_id,
            ArticleImage.slot_index == slot_index,
        )
    ).all()
    for img in imgs:
        session.delete(img)

    # 从正文中移除对应标记（按 slot_index 顺序找第 N 个标记）
    import re
    pattern = re.compile(r'\n?\[插图(?:位|位置)?[：:].+?\]\n?')
    matches = list(pattern.finditer(article.body))
    if 0 <= slot_index < len(matches):
        m = matches[slot_index]
        article.body = article.body[:m.start()] + article.body[m.end():]
    article.updated_at = _now()
    session.add(article)
    session.commit()
    session.refresh(article)

    # 重排剩余 slot_index
    _reindex_slots_by_body(session, article)

    if request.headers.get("HX-Request") == "true":
        topic = session.get(Topic, article.topic_id)
        campaigns = session.exec(select(Campaign).where(Campaign.brand_id == topic.brand_id)).all() if topic else []
        return _article_detail_fragment(request, article, topic, campaigns, session, force_editing=True)
    return RedirectResponse(f"/writing/articles/{article_id}", status_code=303)


def _find_text_segment_end(body: str, text: str) -> int:
    """在 body 中定位某个 text 段的结束字符位置（用于占位标记插入点）。

    text 是 _split_body_by_slots 切出的纯净文本（已 strip）。
    找 text 在 body 中最后一次出现的位置的末尾；找不到则追加到 body 末尾。
    """
    if not text:
        return len(body)
    pos = body.rfind(text)
    if pos < 0:
        # 文本可能被 strip 过，尝试找首行
        first_line = text.splitlines()[0].strip() if text.splitlines() else text
        pos = body.rfind(first_line)
        if pos < 0:
            return len(body)
        return pos + len(first_line)
    return pos + len(text)


_PLATFORM_STYLE = {
    "小红书": "发布平台：小红书。文风要求：口语化、亲切、多用 emoji 表情符号点缀（每段 1-2 个），段落短小精悍（每段不超过 3 行），开头要有强钩子（提问/感叹/反差），结尾引导互动（点赞/收藏/评论）。可适当使用 hashtag 标签。",
    "微信公众号": "发布平台：微信公众号。文风要求：正式但不失温度，段落结构清晰，有起承转合，用词精准，适当使用小标题分隔段落，开头引人入胜，结尾有余韵或有价值升华。",
}

_IMAGE_SLOT_MARK = "[插图："

_PLATFORM_IMG_HINT = {
    "小红书": "配图风格偏向：色彩明快、年轻化、适合手机竖屏浏览。",
    "微信公众号": "配图风格偏向：质感高级、构图稳重、横版为主。",
}


def _resolve_style_text(style: Style | None, platform: str) -> str:
    """决定写作风格文本：
    - 有默认风格 → 用默认风格（不注入平台特点，避免冲突）
    - 无默认风格但选了平台 → 用平台特点作为写作风格
    - 都没有 → 提示使用品牌内容要求
    """
    if style:
        return style.summary
    if platform in _PLATFORM_STYLE:
        return _PLATFORM_STYLE[platform]
    return "无默认风格，使用品牌内容要求。"


# 无默认风格时，在 prompt 末尾强化平台文风约束（注意力最高位）
_PLATFORM_ENFORCE = {
    "小红书": "⚠️ 严格遵循小红书文风（上述【写作风格】要求）：每段不超过 3 行、每段 1-2 个 emoji、口语化亲切、开头强钩子、结尾引导互动。禁止写成正式长段落或学术腔。",
    "微信公众号": "⚠️ 严格遵循公众号文风（上述【写作风格】要求）：正式有温度、小标题分隔、起承转合、用词精准、结尾余韵。禁止口语化或堆砌 emoji。",
}


def _platform_enforce(style: Style | None, platform: str) -> str:
    """无默认风格 + 选了平台时，返回末尾强化约束；否则空。"""
    if style:
        return ""
    return _PLATFORM_ENFORCE.get(platform, "")


def _platform_directive(platform: str, word_count: int) -> str:
    """目标字数指令（平台文风由 _resolve_style_text 按需注入，此处只管字数）。"""
    if word_count > 0:
        return f"目标字数约 {word_count} 字（允许 ±20% 浮动）。"
    return ""


def _image_slot_directive(platform: str) -> str:
    """多插图位置标记指令。"""
    hint = _PLATFORM_IMG_HINT.get(platform, "")
    return f"""在文章正文中，请在合适的位置插入插图标记，**严格**使用格式 {_IMAGE_SLOT_MARK}插图描述]（注意：「插图」二字后紧跟半角或全角冒号，不要写成「插图位」或「插图位置」）。
插图描述要具体（如「自然光下棉麻织物的纤维肌理」），用于后续 AI 配图。
根据文章长度和内容，自动决定插入 2-4 张插图，均匀分布在文章中。{hint}
不要把所有插图都放在开头或结尾，要穿插在正文段落之间。"""


def _article_prompt(topic: Topic, ctx: KnowledgeContext, style: Style | None,
                    platform: str = "", word_count: int = 0,
                    writing_experience: str = "", writing_req: str = "") -> str:
    style_text = _resolve_style_text(style, platform)
    platform_dir = _platform_directive(platform, word_count)
    img_slot_dir = _image_slot_directive(platform)
    enforce = _platform_enforce(style, platform)
    req_block = f"【写作要求】\n{writing_req.strip()}\n\n" if writing_req and writing_req.strip() else ""
    knowledge_block = knowledge_context_block(ctx, writing_experience)
    default = """{req_block}你是③写作引擎，请基于已采纳选题生成一篇可直接编辑的中文图文稿。

【选题】
标题：{topic.title}
纲要：{topic.outline}
切入角度：{topic.angle}
受众：{topic.audience}
素材：{topic.materials}
时效：{topic.timeliness}
发布时间：{topic.publish_window}

{knowledge_block}

【写作风格】
{style_text}

{platform_dir}

{img_slot_dir}

{enforce}

请输出：
标题：...

正文：...

【输出格式硬约束】
1. 纯文本输出，禁止任何 Markdown 标记：不要用 **加粗**、## 标题、--- 分隔线、`代码块`、> 引用 等。
2. 用中文标点和空行分段，不要用 Markdown 语法制造视觉层次。
3. 换行用单个 \\n，段落之间空一行；不要用 \\r\\n。
4. [插图：...] 标记只能放在完整段落之间，禁止插到句子中间或段落内部。
5. 直接输出正文，不要输出「正文：」之外的解释性文字。
"""
    return resolve("writing:article_prompt", default,
                   topic=topic, style_text=style_text, platform_dir=platform_dir,
                   img_slot_dir=img_slot_dir, enforce=enforce, req_block=req_block,
                   knowledge_block=knowledge_block)


def _article_prompt_with_brief(topic: Topic, ctx: KnowledgeContext, style: Style | None,
                               brief: str, platform: str = "", word_count: int = 0,
                               writing_experience: str = "", writing_req: str = "") -> str:
    """带辩论简报的文章生成 prompt。"""
    style_text = _resolve_style_text(style, platform)
    platform_dir = _platform_directive(platform, word_count)
    img_slot_dir = _image_slot_directive(platform)
    enforce = _platform_enforce(style, platform)
    req_block = f"【写作要求】\n{writing_req.strip()}\n\n" if writing_req and writing_req.strip() else ""
    knowledge_block = knowledge_context_block(ctx, writing_experience)
    default = """{req_block}你是③写作引擎，请基于已采纳选题和辩论简报生成一篇可直接编辑的中文图文稿。

【选题】
标题：{topic.title}
纲要：{topic.outline}
切入角度：{topic.angle}
受众：{topic.audience}
素材：{topic.materials}
时效：{topic.timeliness}
发布时间：{topic.publish_window}

{knowledge_block}

【写作风格】
{style_text}

【辩论综合写作简报】
{brief}

{platform_dir}

{img_slot_dir}

{enforce}

请严格按照辩论简报的切入角度和结构建议写作。标题请采用辩论简报中推荐的文章标题（可微调），不要直接照搬选题标题。请输出：
标题：...

正文：...

【输出格式硬约束】
1. 纯文本输出，禁止任何 Markdown 标记：不要用 **加粗**、## 标题、--- 分隔线、`代码块`、> 引用 等。
2. 用中文标点和空行分段，不要用 Markdown 语法制造视觉层次。
3. 换行用单个 \\n，段落之间空一行；不要用 \\r\\n。
4. [插图：...] 标记只能放在完整段落之间，禁止插到句子中间或段落内部。
5. 直接输出正文，不要输出「正文：」之外的解释性文字。
"""
    return resolve("writing:article_prompt_with_brief", default,
                   topic=topic, brief=brief, style_text=style_text, platform_dir=platform_dir,
                   img_slot_dir=img_slot_dir, enforce=enforce, req_block=req_block,
                   knowledge_block=knowledge_block)


def _parse_image_slots(body: str) -> list[tuple[int, str]]:
    """从文章正文中解析插图标记，返回 [(位置在 body 中的字符偏移, 描述), ...]。

    兼容 LLM 常见变体：[插图：...]、[插图位：...]、[插图位置：...]。
    """
    import re
    pattern = re.compile(r'\[插图(?:位|位置)?[：:](.+?)\]')
    return [(m.start(), m.group(1).strip()) for m in pattern.finditer(body)]


def _strip_image_slots(body: str) -> str:
    """从文章正文中移除插图标记（兼容 [插图：]、[插图位：]、[插图位置：] 变体），保留纯净正文。"""
    import re
    return re.sub(r'\[插图(?:位|位置)?[：:].+?\]\n*', '\n', body).strip()


def _storage_url(path: str) -> str:
    """把 DATA_DIR 下的本地文件路径转成写作模块可访问 URL。"""
    rel = os.path.relpath(os.path.realpath(path), os.path.realpath(config.DATA_DIR))
    return f"/writing/uploads/{quote(rel, safe='/')}"


def _public_image_url(url_or_path: str) -> str:
    """把本地生成图片路径规范化为浏览器可访问 URL，远程 URL 原样保留。"""
    value = (url_or_path or "").strip()
    if not value:
        return ""
    if value.startswith(("http://", "https://", "data:", "/writing/uploads/", "/static/")):
        return value
    real = os.path.realpath(value)
    data_root = os.path.realpath(config.DATA_DIR)
    if real == data_root or real.startswith(data_root + os.sep):
        return _storage_url(real)
    return value


def _slot_desc(article: Article, slot_index: int, session: Session) -> str:
    slots = _parse_image_slots(article.body)
    if slots and slot_index < len(slots):
        return slots[slot_index][1]
    existing = session.exec(
        select(ArticleImage).where(
            ArticleImage.article_id == article.id,
            ArticleImage.slot_index == slot_index,
        ).order_by(ArticleImage.id)
    ).first()
    return existing.slot_desc if existing else ""


def _select_slot_image(session: Session, article: Article, selected: ArticleImage) -> None:
    """标记某个 slot 的当前图，并同步文章主图字段。

    所有 expected slot 都选好后自动切「待审核」。
    """
    peers = session.exec(
        select(ArticleImage).where(
            ArticleImage.article_id == selected.article_id,
            ArticleImage.slot_index == selected.slot_index,
        )
    ).all()
    for img in peers:
        img.is_selected = img.id == selected.id
        session.add(img)
    all_imgs = session.exec(
        select(ArticleImage).where(ArticleImage.article_id == article.id)
    ).all()
    first_selected = session.exec(
        select(ArticleImage).where(
            ArticleImage.article_id == article.id,
            ArticleImage.is_selected == True,
        ).order_by(ArticleImage.slot_index, ArticleImage.id)
    ).first()
    if first_selected:
        article.image_url = first_selected.image_url
        article.image_prompt = first_selected.prompt
    # 注意：单个 slot 选图不自动切「待审核」，让用户能继续换选其他 slot；
    # 全部选好后由用户点「完成配图」按钮显式确认（/confirm-images）。
    article.updated_at = _now()
    session.add(article)
    session.commit()


def _prune_slot_images(session: Session, article_id: int, slot_index: int,
                       keep_id: int | None, max_count: int = 4) -> None:
    """每个 slot 最多保留 max_count 张展示图，优先保留选中图和新上传图。"""
    images = session.exec(
        select(ArticleImage).where(
            ArticleImage.article_id == article_id,
            ArticleImage.slot_index == slot_index,
        ).order_by(ArticleImage.is_selected.desc(), ArticleImage.id.desc())
    ).all()
    kept = 0
    for img in images:
        if img.id == keep_id or kept < max_count:
            kept += 1
            continue
        session.delete(img)
    session.commit()


def _expected_slot_indexes(body: str, images: list[ArticleImage] | None = None) -> set[int]:
    """正文标记和已生成候选图共同决定需要用户选图的 slot。"""
    marked = set(range(len(_parse_image_slots(body))))
    image_slots = {img.slot_index for img in (images or [])}
    return marked | image_slots


def _has_pending_image_candidates(body: str, slots: dict[int, list[ArticleImage]]) -> bool:
    """是否还有 slot 未生成满 4 张候选图，用于待配图页面继续轮询。

    slots 全空 → 自配图模式（用户不上传），不需要轮询。
    手动上传的 slot（所有图都是 prompt='手动上传'）→ 用户自选，不需要补 4 张候选，跳过。
    """
    expected = _expected_slot_indexes(body)
    if not expected and slots:
        expected = set(slots.keys())
    if not expected:
        return True
    if all(len(slots.get(idx, [])) == 0 for idx in expected):
        return False
    for idx in expected:
        imgs = slots.get(idx, [])
        if not imgs:
            return True  # expected 但无图 → AI 还没生成
        # 手动上传的 slot 不需要补候选
        if all(im.prompt == "手动上传" for im in imgs):
            continue
        if len(imgs) < 4:
            return True
    return False


def _all_slots_selected(body: str, images: list[ArticleImage]) -> bool:
    """所有 expected slot 是否都已选好图（用于显示「完成配图」按钮）。"""
    expected = _expected_slot_indexes(body, images)
    if not expected:
        return False
    selected_slots = {img.slot_index for img in images if img.is_selected}
    return expected.issubset(selected_slots)


def _has_missing_slots(body: str, images: list[ArticleImage]) -> bool:
    """是否有 expected slot 完全没有候选图（用于显示「补生缺失配图」按钮）。

    只在 AI 配图模式（正文有插图标记）下有意义；自配图模式返回 False。
    """
    marked = _parse_image_slots(body)
    if not marked:
        return False  # 自配图模式，不提示
    expected = set(range(len(marked)))
    existing_slots = {img.slot_index for img in images}
    return bool(expected - existing_slots)


def _all_image_slots_full(body: str, images: list[ArticleImage]) -> bool:
    """所有 expected slot 是否都已生成满 4 张候选图（用于「待配图」→「待审核」切换判断）。

    自配图模式（正文无插图标记且无候选图）返回 True（视作完成，等用户上传）。
    """
    expected = _expected_slot_indexes(body, images)
    if not expected:
        return True
    counts: dict[int, int] = {}
    for img in images:
        counts[img.slot_index] = counts.get(img.slot_index, 0) + 1
    return all(counts.get(idx, 0) >= 4 for idx in expected)


def _maybe_resume_stalled_image_worker(article: Article, images: list[ArticleImage]) -> bool:
    """检测「待配图」状态卡住（worker 死了）：超过 5 分钟无更新且有缺失 slot → 触发补生。

    返回 True 表示刚触发了补生（调用方应刷新 images 后再渲染）。
    幂等：互斥锁保证不会重复触发。
    """
    if article.status != "待配图":
        return False
    if not _has_missing_slots(article.body, images):
        return False
    # 超过 5 分钟没更新 = worker 卡住
    age = (_now() - article.updated_at).total_seconds()
    if age < 300:
        return False
    t = threading.Thread(
        target=_run_image_worker,
        args=(article.id, article.topic_id, article.platform, True),
        daemon=True,
    )
    t.start()
    return True


def _split_body_by_slots(body: str, slots: dict[int, list[ArticleImage]] | None = None) -> list[dict]:
    """把正文按插图标记切片，返回段落序列供图文混排展示。

    兼容 [插图：...]、[插图位：...]、[插图位置：...] 变体。
    返回 [{"type": "text", "text": "..."}, {"type": "slot", "slot_index": 0, "desc": "..."}] 交替序列。
    slot_index 从 0 递增，对应 ArticleImage.slot_index。

    会修复 LLM 把标记插到句子中间的问题：若标记前的文本不以段落结束符结尾、
    标记后的文本不以换行开头，视为句中插入，把标记移到该完整句子的末尾，
    避免正文被切成碎片显示成"乱码"。
    """
    pattern = re.compile(r'\[插图(?:位|位置)?[：:](.+?)\]')
    result: list[dict] = []
    last_end = 0
    slot_idx = 0
    for m in pattern.finditer(body):
        # 标记前的文本段
        text = body[last_end:m.start()].strip()
        if text:
            result.append({"type": "text", "text": text})
        # 标记本身 → slot 占位
        result.append({"type": "slot", "slot_index": slot_idx, "desc": m.group(1).strip()})
        slot_idx += 1
        last_end = m.end()
    # 末尾文本
    tail = body[last_end:].strip()
    if tail:
        result.append({"type": "text", "text": tail})
    # 如果模型没按要求插入插图标记，但后端兜底生成了候选图，也要在正文末尾展示选图位。
    if slot_idx == 0 and slots:
        for idx in sorted(slots):
            imgs = slots.get(idx) or []
            desc = next((img.slot_desc for img in imgs if img.slot_desc), "文章配图")
            result.append({"type": "slot", "slot_index": idx, "desc": desc})
    # 修复"标记插在句子中间"：把被切断的碎片合并回完整段落，标记移到段落末尾。
    result = _rejoin_split_sentences(result)
    return result


def _rejoin_split_sentences(segs: list[dict]) -> list[dict]:
    """合并被插图标记从句子中间切断的文本碎片。

    判定"句中插入"：text 段不以段落结束符结尾 + 后续紧跟 slot + slot 后的 text 段
    不以换行/新句开头。此时把前后 text 合并、slot 移到合并段之后。
    迭代直到无可合并项。
    """
    # 段落结束符：句末标点、换行、省略号等
    end_re = re.compile(r'[。！？!?…\n…]["」』"』)）]?$')
    # 新句/新段开头：换行、引号开头、列表标记等
    start_re = re.compile(r'^[\n"「『（(\-•]')

    def _ends_paragraph(text: str) -> bool:
        t = text.rstrip()
        if not t:
            return True
        return bool(end_re.search(t[-1:] if len(t) == 1 else t[-2:]))

    def _starts_paragraph(text: str) -> bool:
        t = text.lstrip()
        if not t:
            return True
        return bool(start_re.search(t[:1]))

    out: list[dict] = list(segs)
    changed = True
    while changed:
        changed = False
        for i in range(len(out) - 2):
            if out[i]["type"] != "text" or out[i + 1]["type"] != "slot" or out[i + 2]["type"] != "text":
                continue
            prev_text = out[i]["text"]
            slot = out[i + 1]
            next_text = out[i + 2]["text"]
            # 标记前的文本以段落结束符结尾 → 标记在段落之间，正常，不合并
            if _ends_paragraph(prev_text):
                continue
            # 标记后的文本以新段落开头 → 标记在段落边界，正常，不合并
            if _starts_paragraph(next_text):
                continue
            # 句中插入：合并 prev + next 为一段，slot 移到合并段之后
            merged = {"type": "text", "text": prev_text + next_text}
            out[i:i + 3] = [merged, slot]
            changed = True
            break
    return out


def _image_prompt_for_slot(topic: Topic, ctx: KnowledgeContext, style: Style | None,
                           slot_desc: str, body: str, platform: str = "") -> str:
    """为某个插图位置生成配图 prompt。

    按 SDXL 最佳实践：以具象画面描述为主体，品牌风格做轻量修饰，避免套话淹没关键描述。
    """
    # 平台配图取向（轻量修饰）
    platform_hint = _PLATFORM_IMG_HINT.get(platform, "")
    # 品牌视觉风格：只取核心一句话（首行/首句），避免整份 markdown 指南挤占 prompt
    style_core = ""
    if ctx.style_digest:
        # 取首行非空内容作为风格锚点
        for line in ctx.style_digest.splitlines():
            line = line.strip().lstrip("#").strip()
            if line:
                style_core = line
                break
    # 文章上下文：取该 slot 前后各 100 字，让模型理解这位置插图的语境
    # 兼容 [插图：]、[插图位：]、[插图位置：] 变体
    import re
    slot_pattern = re.compile(r'\[插图(?:位|位置)?[：:]' + re.escape(slot_desc) + r'\]')
    m = slot_pattern.search(body)
    if m:
        slot_pos = m.start()
        context = body[max(0, slot_pos - 100):m.end() + 100]
    else:
        context = body[:200]

    # 预计算条件部分（含分隔符，空则省略，等价于原 parts 过滤拼接）
    atmosphere_part = f"，画面氛围：{style_core}" if style_core else ""
    platform_part = f"，{platform_hint}" if platform_hint else ""
    default = "{slot_desc}{atmosphere_part}{platform_part}，文章语境：…{context}…"
    result = resolve("writing:image_prompt_for_slot", default,
                     slot_desc=slot_desc, atmosphere_part=atmosphere_part,
                     platform_part=platform_part, context=context)
    return result[:1400]
