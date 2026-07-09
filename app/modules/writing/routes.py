"""③写作引擎：采纳选题 → 风格注入 → 文章 → 配图。

边界：读② Topic(status='采纳')，写③ Article/Style；不回写 Topic.status。
"""
import threading
import os
import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
from starlette.requests import Request

from app.core import auth, config, db, llm, sources, storage
from app.core.db import get_session
from app.modules.feedback.experience import experience_pack_text
from app.modules.knowledge.models import Brand, Campaign
from app.modules.topic.contract import KnowledgeContext
from app.modules.topic.models import Topic
from app.modules.writing.debate import clean_llm_output, knowledge_context_block, rewrite_prompt, run_ai_review, run_debate, run_review
from app.modules.writing.models import ARTICLE_STATUSES, PLATFORMS, STYLE_SOURCES, Article, ArticleImage, DebateRecord, Style, _now

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


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


def _first_brand(session: Session) -> Brand | None:
    return session.exec(select(Brand).order_by(Brand.id)).first()


def _default_style(session: Session, campaign_id: int | None) -> Style | None:
    if campaign_id is None:
        return None
    style = session.exec(
        select(Style).where(Style.campaign_id == campaign_id, Style.is_default == True).order_by(Style.id)
    ).first()
    if style is not None:
        return style
    return session.exec(select(Style).where(Style.campaign_id == campaign_id).order_by(Style.id)).first()


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


def _style_from_hit(campaign_id: int, hit: dict, is_default: bool) -> Style:
    title = (hit.get("title") or "写作风格").strip()
    summary = (hit.get("summary") or title).strip()
    source = (hit.get("source") or "stub").strip()
    return Style(
        campaign_id=campaign_id,
        name=title[:80],
        summary=summary[:1200],
        reference_url=(hit.get("url") or "").strip(),
        source=source,
        is_default=is_default,
    )


def _preset_prompt(campaign: Campaign, ctx: KnowledgeContext, count: int) -> str:
    """预设风格 prompt：基于知识库品牌调性/活动内容，让 LLM 生成符合主题的写作风格。"""
    return f"""你是写作风格预设器。请基于以下知识库信息，为活动「{campaign.name}」预设 {count} 个符合主题的写作风格。

【品牌调性】
{ctx.brand_prompt or "（未设置）"}

【内容要求】
{ctx.content_notes or "（未设置）"}

【品牌资料综合】
{ctx.doc_digest or "（无）"}

【活动简报】
{ctx.campaign_digest or "（品牌常青，无特定活动）"}

请预设 {count} 个差异化的写作风格，每个风格包含名称和总结（段落结构/语气调性/用词偏好/节奏，100-200字）。
严格按以下格式输出，每个风格之间用空行分隔，不要输出思考过程、分析步骤或其他任何内容：

名称：风格名称
总结：段落结构、语气调性、用词偏好等总结

名称：另一个风格
总结：...
"""


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
            out.append((name[:80], "\n".join(summary_lines)[:1200]))
    return out


def _extract_style_prompt(url: str, text: str) -> str:
    """URL 提取 prompt：从网页正文提炼一个可复用的写作风格。"""
    return f"""请分析以下网页内容的写作风格，提炼出一个可复用的写作风格总结。

【来源URL】
{url}

【网页正文】
{text[:6000]}

直接按以下格式输出，不要输出思考过程、分析步骤或其他任何内容：
名称：用一个短语概括这种风格
总结：段落结构、语气调性、用词偏好、节奏等（100-200字）
"""


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
    tab = request.query_params.get("tab", "preset")
    if tab not in ("preset", "library", "new"):
        tab = "preset"
    highlight_raw = request.query_params.get("highlight")
    try:
        highlight = int(highlight_raw) if highlight_raw else None
    except ValueError:
        highlight = None
    brand = _first_brand(session)
    topics: list[Topic] = []
    campaigns: list[Campaign] = []
    article_rows: list[dict] = []
    topic_articles: dict[int, Article] = {}
    styles: dict[int, list[Style]] = {}
    preset_styles: dict[int, list[Style]] = {}
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
                if article.topic_id not in topic_articles:
                    topic_articles[article.topic_id] = article
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
        for c in campaigns:
            all_styles = session.exec(
                select(Style).where(Style.campaign_id == c.id).order_by(Style.is_default.desc(), Style.id)
            ).all()
            preset_styles[c.id] = [st for st in all_styles if st.source == "preset"]
            styles[c.id] = [st for st in all_styles if st.source != "preset"]
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
        "preset_styles": preset_styles,
        "style_sources": STYLE_SOURCES,
        "catalog": sources.catalog(),
        "tab": tab,
        "highlight": highlight,
        "level": getattr(request.state, "level", 0),
        "platforms": PLATFORMS,
    })


def _extract_from_hit_prompt(hit: dict) -> str:
    """网络抓取风格提取 prompt：基于搜索命中（标题+摘要+URL）提炼可复用的写作风格。"""
    title = (hit.get("title") or "").strip()
    summary = (hit.get("summary") or "").strip()
    url = (hit.get("url") or "").strip()
    return f"""请分析以下搜索结果内容，提炼出一个可复用的写作风格总结。

【来源标题】
{title}

【来源摘要】
{summary}

【来源URL】
{url}

直接按以下格式输出，不要输出思考过程、分析步骤或其他任何内容：
名称：用一个短语概括这种风格
总结：段落结构、语气调性、用词偏好、节奏等（100-200字）
"""


@router.post("/writing/styles/campaign/{campaign_id}/capture")
def capture_styles(campaign_id: int, request: Request, query: str = Form(""),
                   source: list[str] = Form([]), count: int = Form(5),
                   session: Session = Depends(get_session)):
    """网络抓取：搜索引擎检索 → 每条命中经 LLM 提炼写作风格 → 入风格库（记录搜索来源）。"""
    auth.require_level(request, 1)
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(404, "活动不存在")
    names = [s for s in source if s in sources.available()] or ["stub"]
    q = query.strip() or f"{campaign.name} 写作风格"
    hits = sources.gather(names, q, per_source=max(1, min(count, 5))) or sources.search("stub", q)
    existing_default = _default_style(session, campaign_id) is not None
    created = 0
    failed = 0
    for hit in hits[:max(1, min(count, 5))]:
        try:
            raw = llm.generate_text(_extract_from_hit_prompt(hit),
                                    task="writing_style_capture", module="writing", fallback=False)
        except RuntimeError:
            failed += 1
            continue
        parsed = _parse_styles(raw)
        if not parsed:
            failed += 1
            continue
        name, summary = parsed[0]
        session.add(Style(
            campaign_id=campaign_id, name=name, summary=summary,
            reference_url=(hit.get("url") or "").strip(),
            source=(hit.get("source") or "stub").strip(),
            is_default=(not existing_default and created == 0),
        ))
        created += 1
    session.commit()
    if created == 0:
        raise HTTPException(502, f"网络抓取到 {len(hits)} 条结果，但 LLM 提取全部失败，请检查模型配置后重试")
    return RedirectResponse("/writing", status_code=303)


@router.post("/writing/styles/{style_id}/default")
def set_default_style(style_id: int, request: Request, session: Session = Depends(get_session)):
    auth.require_level(request, 1)
    style = session.get(Style, style_id)
    if style is None:
        raise HTTPException(404, "风格不存在")
    peers = session.exec(select(Style).where(Style.campaign_id == style.campaign_id)).all()
    for peer in peers:
        peer.is_default = peer.id == style_id
        session.add(peer)
    session.commit()
    return RedirectResponse("/writing", status_code=303)


@router.post("/writing/styles/campaign/{campaign_id}/unset-default")
def unset_default_style(campaign_id: int, request: Request, session: Session = Depends(get_session)):
    """取消该活动的默认风格——所有风格 is_default=False，让 AI 自行决定文风。"""
    auth.require_level(request, 1)
    peers = session.exec(select(Style).where(Style.campaign_id == campaign_id)).all()
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


@router.post("/writing/styles/campaign/{campaign_id}/preset")
def preset_styles(campaign_id: int, request: Request, count: int = Form(3),
                  session: Session = Depends(get_session)):
    """预设：基于知识库（品牌调性/活动简报）调 LLM 生成符合主题的写作风格。"""
    auth.require_level(request, 1)
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(404, "活动不存在")
    brand = session.get(Brand, campaign.brand_id)
    if brand is None:
        raise HTTPException(400, "品牌不存在")
    n = max(1, min(count, 20))
    ctx = KnowledgeContext.load(session, brand.id, campaign_id)
    prompt = _preset_prompt(campaign, ctx, n)
    try:
        raw = llm.generate_text(prompt, task="writing_style_preset", module="writing", fallback=False)
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    parsed = _parse_styles(raw)
    if not parsed:
        raise HTTPException(502, "AI 未返回可识别的风格，请重试或检查模型配置")
    existing_default = _default_style(session, campaign_id) is not None
    for i, (name, summary) in enumerate(parsed[:n]):
        session.add(Style(
            campaign_id=campaign_id, name=name, summary=summary,
            source="preset", is_default=(not existing_default and i == 0),
        ))
    session.commit()
    return RedirectResponse("/writing", status_code=303)


@router.post("/writing/styles/campaign/{campaign_id}/extract")
def extract_style(campaign_id: int, request: Request, url: str = Form(...),
                  session: Session = Depends(get_session)):
    """新建·URL 提取：抓 URL 页面正文 → LLM 提炼写作风格。"""
    auth.require_level(request, 1)
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(404, "活动不存在")
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
    existing_default = _default_style(session, campaign_id) is not None
    style = Style(
        campaign_id=campaign_id, name=name, summary=summary,
        reference_url=url, source="url",
        is_default=not existing_default,
    )
    session.add(style)
    session.commit()
    session.refresh(style)
    return RedirectResponse(f"/writing?tab=library&highlight={style.id}", status_code=303)


@router.post("/writing/topics/{topic_id}/generate")
def generate_article(topic_id: int, request: Request,
                      debate_rounds: int = Form(2), review_rounds: int = Form(2),
                      platform: str = Form(""), word_count: int = Form(0),
                      ai_images: str = Form(""), use_experience: str = Form(""),
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
    use_experience_flag = use_experience.strip().lower() in ("true", "1", "yes", "on")
    article = session.exec(
        _active_article_query().where(Article.topic_id == topic.id).order_by(Article.updated_at.desc())
    ).first()
    if article is None:
        article = Article(topic_id=topic.id, campaign_id=topic.campaign_id, title=topic.title, status="辩论中")
    elif article.status in RUNNING_ARTICLE_STATUSES and not article.error_message:
        if request.headers.get("HX-Request") == "true":
            campaigns = session.exec(select(Campaign).where(Campaign.brand_id == topic.brand_id)).all()
            records = session.exec(
                select(DebateRecord).where(DebateRecord.article_id == article.id)
                .order_by(DebateRecord.round_num, DebateRecord.id)
            ).all()
            return templates.TemplateResponse(request, "writing/_generate_response.html", {
                "request": request,
                "t": topic,
                "topic": topic,
                "article": article,
                "campaigns": campaigns,
                "cmap": {c.id: c.name for c in campaigns},
                "campaign_name": {c.id: c.name for c in campaigns}.get(topic.campaign_id, "品牌常青"),
                "level": getattr(request.state, "level", 0),
                "records": records,
                "oob": True,
                "display_phase": _display_phase_for_article(article),
                "platforms": PLATFORMS,
            })
        return RedirectResponse("/writing", status_code=303)
    # 清空旧辩论记录 + 旧候选图（重新生成时，避免残留与新正文错位）
    old_records = session.exec(
        select(DebateRecord).where(DebateRecord.article_id == article.id)
    ).all() if article.id else []
    for r in old_records:
        session.delete(r)
    if article.id:
        for oi in session.exec(
            select(ArticleImage).where(ArticleImage.article_id == article.id)
        ).all():
            session.delete(oi)
    article.status = "辩论中" if dr > 0 else "写作中"
    article.error_message = ""
    article.debate_rounds = dr
    article.review_rounds = rr
    article.debate_brief = ""
    article.review_summary = ""
    article.image_url = ""
    article.image_prompt = ""
    article.platform = pf
    article.word_count = wc
    article.updated_at = _now()
    session.add(article)
    session.commit()
    session.refresh(article)
    # 后台线程跑完整流程
    t = threading.Thread(
        target=_run_generation_worker,
        args=(article.id, topic.id, dr, rr, pf, wc, ai_images_flag, use_experience_flag),
        daemon=True,
    )
    t.start()

    if request.headers.get("HX-Request") == "true":
        # HTMX：左侧刷新原选题卡片，右侧 OOB 追加生成中卡片
        campaigns = session.exec(select(Campaign).where(Campaign.brand_id == topic.brand_id)).all()
        return templates.TemplateResponse(request, "writing/_generate_response.html", {
            "request": request,
            "t": topic,
            "topic": topic,
            "article": article,
            "campaigns": campaigns,
            "cmap": {c.id: c.name for c in campaigns},
            "campaign_name": {c.id: c.name for c in campaigns}.get(topic.campaign_id, "品牌常青"),
            "level": getattr(request.state, "level", 0),
            "records": [],
            "oob": True,
            "display_phase": None,
            "platforms": PLATFORMS,
        })
    return RedirectResponse("/writing", status_code=303)


def _run_generation_worker(article_id: int, topic_id: int, debate_rounds: int, review_rounds: int,
                           platform: str = "", word_count: int = 0, ai_images: bool = True,
                           use_experience: bool = False) -> None:
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
            style = _default_style(s, topic.campaign_id)
            style_text = style.summary if style else "无默认风格，使用品牌内容要求。"
            writing_experience = (
                experience_pack_text(s, topic.brand_id, topic.campaign_id, "写作经验", platform)
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
                    topic, ctx, style, brief, platform, word_count, writing_experience)
            else:
                prompt = _article_prompt(topic, ctx, style, platform, word_count, writing_experience)

            # ── 生成文本 ──
            article.status = "写作中"
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
                rewrite_p = rewrite_prompt(article, review_summary, topic, ctx, style_text, writing_experience)
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
                style = _default_style(s, topic.campaign_id)

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


def _run_single_slot_worker(article_id: int, topic_id: int, slot_index: int,
                             slot_desc: str, platform: str = "") -> None:
    """配图子线程：为指定 slot 重新生成 4 张候选图。

    清理该 slot 的旧候选图，保留其他 slot 的图。
    用 minimax n 参数批量生成 4 张（1 次 API 调用）。
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
                    return
                ctx = KnowledgeContext.load(s, topic.brand_id, topic.campaign_id)
                style = _default_style(s, topic.campaign_id)

                # 清理该 slot 的旧候选图
                old_imgs = s.exec(
                    select(ArticleImage).where(
                        ArticleImage.article_id == article_id,
                        ArticleImage.slot_index == slot_index,
                    )
                ).all()
                for oi in old_imgs:
                    s.delete(oi)
                s.commit()

                img_p = _image_prompt_for_slot(topic, ctx, style, slot_desc, article.body, platform)
                try:
                    urls = llm.generate_images(img_p, module="writing", n=4, fallback=False)
                except RuntimeError:
                    urls = []
                for candidate_idx, url in enumerate(urls):
                    # 默认选中第 0 张：AI 给默认选择，用户不换 = 默认认可
                    s.add(ArticleImage(
                        article_id=article_id, prompt=img_p, image_url=_public_image_url(url),
                        slot_index=slot_index, slot_desc=slot_desc,
                        is_selected=(candidate_idx == 0),
                    ))
                s.commit()

                # 单 slot 重生后：若所有 slot 满 4 张 → 待审核；否则保持待配图
                all_imgs = s.exec(
                    select(ArticleImage).where(ArticleImage.article_id == article_id)
                ).all()
                if _all_image_slots_full(article.body, all_imgs):
                    article.status = "待审核"
                article.updated_at = _now()
                s.add(article)
                s.commit()

            except Exception as exc:
                s.rollback()
                # 单 slot 失败不影响整体，记日志即可（article 状态不变）
                print(f"[single-slot-worker] article={article_id} slot={slot_index} 失败: {exc}", flush=True)
    finally:
        lock.release()


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
def detail_status(article_id: int, request: Request, session: Session = Depends(get_session)):
    """HTMX 轮询（详情页用）：辩论/写作/重写中 → 更新辩论过程；待配图 → 选图界面；待审核 → 最终文章+换选。"""
    article = session.get(Article, article_id)
    if article is None:
        return RedirectResponse("/writing", status_code=303)
    topic = session.get(Topic, article.topic_id)
    if topic is None:
        return RedirectResponse("/writing", status_code=303)
    campaigns = session.exec(select(Campaign).where(Campaign.brand_id == topic.brand_id)).all()
    return _article_detail_fragment(request, article, topic, campaigns, session)


@router.get("/writing/uploads/{rel_path:path}")
def writing_upload(rel_path: str, request: Request):
    """读取写作模块上传图片。走应用登录中间件，不直接暴露整个 data 目录。"""
    auth.require_level(request, 0)
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
    old_imgs = session.exec(
        select(ArticleImage).where(
            ArticleImage.article_id == article_id,
            ArticleImage.slot_index == slot_index,
        )
    ).all()
    for oi in old_imgs:
        session.delete(oi)
    article.updated_at = _now()
    session.add(article)
    session.commit()
    # 启动子线程异步重生该 slot
    t = threading.Thread(
        target=_run_single_slot_worker,
        args=(article_id, article.topic_id, slot_index, slot_desc, article.platform),
        daemon=True,
    )
    t.start()
    # 立即返回当前详情片段（旧图已被清理，新图生成中，轮询会自动补上）
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


@router.post("/writing/articles/{article_id}/edit-body")
def edit_article_body(article_id: int, request: Request,
                      body: str = Form(...), title: str = Form(""),
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
    new_body = (body or "").strip()
    if not new_body:
        raise HTTPException(400, "正文不能为空")
    article.body = new_body
    new_title = (title or "").strip()
    if new_title:
        article.title = new_title[:200]
    article.updated_at = _now()
    session.add(article)
    session.commit()
    session.refresh(article)
    # 正文标记顺序可能变了 → 重排 slot_index
    _reindex_slots_by_body(session, article)
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
插图描述要具体（如「敦煌飞天的色彩渐变示意」），用于后续 AI 配图。
根据文章长度和内容，自动决定插入 2-4 张插图，均匀分布在文章中。{hint}
不要把所有插图都放在开头或结尾，要穿插在正文段落之间。"""


def _article_prompt(topic: Topic, ctx: KnowledgeContext, style: Style | None,
                    platform: str = "", word_count: int = 0,
                    writing_experience: str = "") -> str:
    style_text = _resolve_style_text(style, platform)
    platform_dir = _platform_directive(platform, word_count)
    img_slot_dir = _image_slot_directive(platform)
    enforce = _platform_enforce(style, platform)
    return f"""你是③写作引擎，请基于已采纳选题生成一篇可直接编辑的中文图文稿。

【选题】
标题：{topic.title}
纲要：{topic.outline}
切入角度：{topic.angle}
受众：{topic.audience}
素材：{topic.materials}
时效：{topic.timeliness}
发布时间：{topic.publish_window}

{knowledge_context_block(ctx, writing_experience)}

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


def _article_prompt_with_brief(topic: Topic, ctx: KnowledgeContext, style: Style | None,
                               brief: str, platform: str = "", word_count: int = 0,
                               writing_experience: str = "") -> str:
    """带辩论简报的文章生成 prompt。"""
    style_text = _resolve_style_text(style, platform)
    platform_dir = _platform_directive(platform, word_count)
    img_slot_dir = _image_slot_directive(platform)
    enforce = _platform_enforce(style, platform)
    return f"""你是③写作引擎，请基于已采纳选题和辩论简报生成一篇可直接编辑的中文图文稿。

【选题】
标题：{topic.title}
纲要：{topic.outline}
切入角度：{topic.angle}
受众：{topic.audience}
素材：{topic.materials}
时效：{topic.timeliness}
发布时间：{topic.publish_window}

{knowledge_context_block(ctx, writing_experience)}

【写作风格】
{style_text}

【辩论综合写作简报】
{brief}

{platform_dir}

{img_slot_dir}

{enforce}

请严格按照辩论简报的切入角度和结构建议写作。请输出：
标题：...

正文：...

【输出格式硬约束】
1. 纯文本输出，禁止任何 Markdown 标记：不要用 **加粗**、## 标题、--- 分隔线、`代码块`、> 引用 等。
2. 用中文标点和空行分段，不要用 Markdown 语法制造视觉层次。
3. 换行用单个 \\n，段落之间空一行；不要用 \\r\\n。
4. [插图：...] 标记只能放在完整段落之间，禁止插到句子中间或段落内部。
5. 直接输出正文，不要输出「正文：」之外的解释性文字。
"""


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

    parts = [
        f"{slot_desc}",  # 具象画面描述（核心）
        f"画面氛围：{style_core}" if style_core else "",
        f"{platform_hint}" if platform_hint else "",
        f"文章语境：…{context}…",
    ]
    prompt = "，".join(p for p in parts if p)
    return prompt[:1400]
