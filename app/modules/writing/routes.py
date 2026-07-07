"""③写作引擎：采纳选题 → 风格注入 → 文章 → 配图。

边界：读② Topic(status='采纳')，写③ Article/Style；不回写 Topic.status。
"""
import threading

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
from starlette.requests import Request

from app.core import auth, db, llm, sources
from app.core.db import get_session
from app.modules.knowledge.models import Brand, Campaign
from app.modules.topic.contract import KnowledgeContext
from app.modules.topic.models import Topic
from app.modules.writing.debate import clean_llm_output, rewrite_prompt, run_debate, run_review
from app.modules.writing.models import ARTICLE_STATUSES, PLATFORMS, STYLE_SOURCES, Article, ArticleImage, DebateRecord, Style, _now

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
RUNNING_ARTICLE_STATUSES = ("辩论中", "写作中", "重写中", "待配图", "待选图")


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
    if status_filter not in (*ARTICLE_STATUSES, "全部"):
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
        elif status_filter == "已删除":
            article_q = article_q.where(Article.status == "已删除")
        else:
            article_q = article_q.where(Article.deleted_at == None, Article.status != "已删除")
            article_q = article_q.where(Article.status == status_filter)
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
        "status_filters": ("全部", *ARTICLE_STATUSES),
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
                      session: Session = Depends(get_session)):
    """生成图文（异步后台）：辩论 → 生成文本 → 多插图候选 → 评审 → 重写 → 待选图。

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
    # 清空旧辩论记录（重新生成时）
    old_records = session.exec(
        select(DebateRecord).where(DebateRecord.article_id == article.id)
    ).all() if article.id else []
    for r in old_records:
        session.delete(r)
    article.status = "辩论中" if dr > 0 else "写作中"
    article.error_message = ""
    article.debate_rounds = dr
    article.review_rounds = rr
    article.debate_brief = ""
    article.review_summary = ""
    article.platform = pf
    article.word_count = wc
    article.updated_at = _now()
    session.add(article)
    session.commit()
    session.refresh(article)
    # 后台线程跑完整流程
    t = threading.Thread(
        target=_run_generation_worker,
        args=(article.id, topic.id, dr, rr, pf, wc),
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
                           platform: str = "", word_count: int = 0) -> None:
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

            # ── 辩论阶段 ──
            if debate_rounds > 0:
                brief = run_debate(s, article_id, debate_rounds, topic, ctx)
                article.debate_brief = brief
                article.updated_at = _now()
                s.add(article)
                s.commit()
                prompt = _article_prompt_with_brief(topic, ctx, style, brief, platform, word_count)
            else:
                prompt = _article_prompt(topic, ctx, style, platform, word_count)

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
                rewrite_p = rewrite_prompt(article, review_summary, topic, ctx, style_text)
                new_body = llm.generate_text(rewrite_p, task="writing_rewrite", module="writing", fallback=False)
                new_body = clean_llm_output(new_body)
                if new_body:
                    article.body = new_body
                    article.title = _article_title(new_body, topic.title)
                    s.add(article)
                    s.commit()
                    s.refresh(article)

            # ── 文本完成：立即提交「待配图」状态，用户可阅读正文 ──
            article.status = "待配图"
            article.error_message = ""
            article.generated_at = _now()
            article.updated_at = article.generated_at
            s.add(article)
            s.commit()

            # 启动配图子线程，异步生成多插图候选
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
                article.status = "已生成" if article.body else "写作中"
                article.error_message = str(exc)[:500]
                article.updated_at = _now()
                s.add(article)
                s.commit()


def _run_image_worker(article_id: int, topic_id: int, platform: str = "") -> None:
    """配图子线程：为已生成的正文生成多插图候选(4张/位置) → 待选图。

    独立 Session，失败写 error_message（不影响已完成的正文）。
    """
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
            # 清理旧候选图
            old_imgs = s.exec(select(ArticleImage).where(ArticleImage.article_id == article_id)).all()
            for oi in old_imgs:
                s.delete(oi)
            s.commit()

            for slot_idx, (_, slot_desc) in enumerate(slots):
                img_p = _image_prompt_for_slot(topic, ctx, style, slot_desc, article.body, platform)
                for _ in range(4):
                    try:
                        url = llm.generate_image(img_p, module="writing", fallback=False)
                        s.add(ArticleImage(
                            article_id=article_id, prompt=img_p, image_url=url,
                            slot_index=slot_idx, slot_desc=slot_desc,
                            is_selected=(slot_idx == 0),
                        ))
                        s.commit()
                    except RuntimeError:
                        continue  # 单张失败跳过，不中断

            article.status = "待选图"
            article.updated_at = _now()
            s.add(article)
            s.commit()

        except Exception as exc:
            s.rollback()
            article = s.get(Article, article_id)
            if article:
                # 配图失败但正文已完成：回到「已生成」让用户查阅，错误记在 error_message
                article.status = "已生成"
                article.error_message = f"配图生成失败：{str(exc)[:400]}"
                article.updated_at = _now()
                s.add(article)
                s.commit()


def _run_single_slot_worker(article_id: int, topic_id: int, slot_index: int,
                             slot_desc: str, platform: str = "") -> None:
    """配图子线程：为指定 slot 重新生成 4 张候选图。

    清理该 slot 的旧候选图，保留其他 slot 的图。
    """
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
            for _ in range(4):
                try:
                    url = llm.generate_image(img_p, module="writing", fallback=False)
                    s.add(ArticleImage(
                        article_id=article_id, prompt=img_p, image_url=url,
                        slot_index=slot_index, slot_desc=slot_desc,
                        is_selected=False,
                    ))
                    s.commit()
                except RuntimeError:
                    continue

            # 配图完成，状态保持「待选图」（让用户选）
            # 如果原来就是「待配图」，切到「待选图」；如果已经是「待选图」，保持
            if article.status == "待配图":
                article.status = "待选图"
            article.updated_at = _now()
            s.add(article)
            s.commit()

        except Exception as exc:
            s.rollback()
            # 单 slot 失败不影响整体，记日志即可（article 状态不变）
            print(f"[single-slot-worker] article={article_id} slot={slot_index} 失败: {exc}", flush=True)


def _display_phase_for_article(article: Article) -> str | None:
    """根据 article 当前持久化状态推导页面应展示的阶段标签。"""
    if article.status in ("辩论中", "写作中", "重写中"):
        return article.status
    if article.status == "已生成" and article.review_rounds > 0 and not article.review_summary:
        return "评审中"
    # 待选图和已完成 → None（generate_status 单独处理待选图）
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
                              campaigns: list[Campaign], session: Session):
    """返回文章详情内容片段（详情页轮询用）：辩论过程 / 选图 / 最终文章。"""
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
        "article_body_clean": "",
        "body_segments": [],
    }
    # 待配图 / 待选图：装载候选图数据 + 图文混排切片
    if article.status in ("待配图", "待选图"):
        images = session.exec(
            select(ArticleImage).where(ArticleImage.article_id == article.id)
            .order_by(ArticleImage.slot_index, ArticleImage.id)
        ).all()
        slots: dict[int, list[ArticleImage]] = {}
        for img in images:
            slots.setdefault(img.slot_index, []).append(img)
        ctx["slots"] = slots
        ctx["article_body_clean"] = _strip_image_slots(article.body)
        ctx["body_segments"] = _split_body_by_slots(article.body)
        return templates.TemplateResponse(request, "writing/_article_detail_content.html", ctx)

    # 正在生成：装载辩论记录
    display_phase = _display_phase_for_article(article)
    if display_phase is not None:
        ctx["records"] = session.exec(
            select(DebateRecord).where(DebateRecord.article_id == article.id)
            .order_by(DebateRecord.round_num, DebateRecord.id)
        ).all()
        ctx["display_phase"] = display_phase
        return templates.TemplateResponse(request, "writing/_article_detail_content.html", ctx)

    # 已完成：装载完整辩论/评审记录供查阅 + 选中的图用于图文混排展示
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
    ctx["body_segments"] = _split_body_by_slots(article.body)
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
        "article_body_clean": "",
        "body_segments": [],
    }
    if article.status in ("待配图", "待选图"):
        images = session.exec(
            select(ArticleImage).where(ArticleImage.article_id == article.id)
            .order_by(ArticleImage.slot_index, ArticleImage.id)
        ).all()
        slots: dict[int, list[ArticleImage]] = {}
        for img in images:
            slots.setdefault(img.slot_index, []).append(img)
        ctx["slots"] = slots
        ctx["article_body_clean"] = _strip_image_slots(article.body)
        ctx["body_segments"] = _split_body_by_slots(article.body)
    elif _display_phase_for_article(article) is not None:
        ctx["records"] = session.exec(
            select(DebateRecord).where(DebateRecord.article_id == article.id)
            .order_by(DebateRecord.round_num, DebateRecord.id)
        ).all()
        ctx["display_phase"] = _display_phase_for_article(article)
    else:
        # 已完成：装载辩论/评审记录 + 选中的图用于图文混排展示
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
        ctx["body_segments"] = _split_body_by_slots(article.body)
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
    """HTMX 轮询（详情页用）：辩论/写作/重写中 → 更新辩论过程；待选图 → 选图界面；已完成 → 最终文章。"""
    article = session.get(Article, article_id)
    if article is None:
        return RedirectResponse("/writing", status_code=303)
    topic = session.get(Topic, article.topic_id)
    if topic is None:
        return RedirectResponse("/writing", status_code=303)
    campaigns = session.exec(select(Campaign).where(Campaign.brand_id == topic.brand_id)).all()
    return _article_detail_fragment(request, article, topic, campaigns, session)


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
    if not slots or slot_index >= len(slots):
        raise HTTPException(400, f"插图位置 {slot_index + 1} 不存在")
    _, slot_desc = slots[slot_index]
    # 启动子线程异步重生该 slot
    t = threading.Thread(
        target=_run_single_slot_worker,
        args=(article_id, article.topic_id, slot_index, slot_desc, article.platform),
        daemon=True,
    )
    t.start()
    # 立即返回当前详情片段（旧图已被清理，新图生成中，轮询会自动补上）
    if request.headers.get("HX-Request") == "true":
        # 先清掉旧候选图，避免页面还显示旧图
        old_imgs = session.exec(
            select(ArticleImage).where(
                ArticleImage.article_id == article_id,
                ArticleImage.slot_index == slot_index,
            )
        ).all()
        for oi in old_imgs:
            session.delete(oi)
        session.commit()
        topic = session.get(Topic, article.topic_id)
        campaigns = session.exec(select(Campaign).where(Campaign.brand_id == topic.brand_id)).all() if topic else []
        return _article_detail_fragment(request, article, topic, campaigns, session)
    return RedirectResponse(f"/writing/articles/{article_id}", status_code=303)


@router.post("/writing/articles/{article_id}/select-images")
def select_images(article_id: int, request: Request,
                  image_id: list[int] = Form([]),
                  session: Session = Depends(get_session)):
    """用户选图：接收每个 slot 选中的 image_id，标记 is_selected，完成后状态改为已生成。"""
    auth.require_level(request, 1)
    article = session.get(Article, article_id)
    if article is None:
        raise HTTPException(404, "文章不存在")
    selected_set = {str(i) for i in image_id}

    # 清除旧选择，标记新选择
    all_imgs = session.exec(select(ArticleImage).where(ArticleImage.article_id == article_id)).all()
    for img in all_imgs:
        img.is_selected = str(img.id) in selected_set
        session.add(img)
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
    article.status = "已生成"
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
    article.status = "已生成" if article.body and article.image_url else "写作中"
    article.updated_at = _now()
    session.add(article)
    session.commit()
    if request.headers.get("HX-Request") == "true":
        topic = session.get(Topic, article.topic_id)
        campaigns = session.exec(select(Campaign).where(Campaign.brand_id == topic.brand_id)).all() if topic else []
        return _article_list_item_fragment(request, article, topic, campaigns)
    return RedirectResponse(f"/writing/articles/{article_id}", status_code=303)


_PLATFORM_STYLE = {
    "小红书": "发布平台：小红书。文风要求：口语化、亲切、多用 emoji 表情符号点缀（每段 1-2 个），段落短小精悍（每段不超过 3 行），开头要有强钩子（提问/感叹/反差），结尾引导互动（点赞/收藏/评论）。可适当使用 hashtag 标签。",
    "微信公众号": "发布平台：微信公众号。文风要求：正式但不失温度，段落结构清晰，有起承转合，用词精准，适当使用小标题分隔段落，开头引人入胜，结尾有余韵或有价值升华。",
}

_IMAGE_SLOT_MARK = "[插图："

_PLATFORM_IMG_HINT = {
    "小红书": "配图风格偏向：色彩明快、年轻化、适合手机竖屏浏览。",
    "微信公众号": "配图风格偏向：质感高级、构图稳重、横版为主。",
}


def _platform_directive(platform: str, word_count: int) -> str:
    """平台风格指令 + 目标字数。"""
    parts = []
    if platform in _PLATFORM_STYLE:
        parts.append(_PLATFORM_STYLE[platform])
    if word_count > 0:
        parts.append(f"目标字数约 {word_count} 字（允许 ±20% 浮动）。")
    return "\n".join(parts)


def _image_slot_directive(platform: str) -> str:
    """多插图位置标记指令。"""
    hint = _PLATFORM_IMG_HINT.get(platform, "")
    return f"""在文章正文中，请在合适的位置插入插图标记，格式为 {_IMAGE_SLOT_MARK}插图描述]。插图描述要具体（如「敦煌飞天的色彩渐变示意」），用于后续 AI 配图。
根据文章长度和内容，自动决定插入 2-4 张插图，均匀分布在文章中。{hint}
不要把所有插图都放在开头或结尾，要穿插在正文段落之间。"""


def _article_prompt(topic: Topic, ctx: KnowledgeContext, style: Style | None,
                    platform: str = "", word_count: int = 0) -> str:
    style_text = style.summary if style else "无默认风格，使用品牌内容要求。"
    platform_dir = _platform_directive(platform, word_count)
    img_slot_dir = _image_slot_directive(platform)
    return f"""你是③写作引擎，请基于已采纳选题生成一篇可直接编辑的中文图文稿。

【选题】
标题：{topic.title}
纲要：{topic.outline}
切入角度：{topic.angle}
受众：{topic.audience}
素材：{topic.materials}
时效：{topic.timeliness}
发布时间：{topic.publish_window}

【知识库】
品牌调性：{ctx.brand_prompt}
内容要求：{ctx.content_notes}
品牌资料综合：{ctx.doc_digest}
活动简报：{ctx.campaign_digest}
数据池资料：{"；".join(ctx.pool_materials)}
经验包：{"；".join(ctx.pool_experiences)}

【默认写作风格】
{style_text}

{platform_dir}

{img_slot_dir}

请输出：
标题：...

正文：...
"""


def _article_prompt_with_brief(topic: Topic, ctx: KnowledgeContext, style: Style | None,
                               brief: str, platform: str = "", word_count: int = 0) -> str:
    """带辩论简报的文章生成 prompt。"""
    style_text = style.summary if style else "无默认风格，使用品牌内容要求。"
    platform_dir = _platform_directive(platform, word_count)
    img_slot_dir = _image_slot_directive(platform)
    return f"""你是③写作引擎，请基于已采纳选题和辩论简报生成一篇可直接编辑的中文图文稿。

【选题】
标题：{topic.title}
纲要：{topic.outline}
切入角度：{topic.angle}
受众：{topic.audience}
素材：{topic.materials}
时效：{topic.timeliness}
发布时间：{topic.publish_window}

【知识库】
品牌调性：{ctx.brand_prompt}
内容要求：{ctx.content_notes}
品牌资料综合：{ctx.doc_digest}
活动简报：{ctx.campaign_digest}
数据池资料：{"；".join(ctx.pool_materials)}
经验包：{"；".join(ctx.pool_experiences)}

【默认写作风格】
{style_text}

【辩论综合写作简报】
{brief}

{platform_dir}

{img_slot_dir}

请严格按照辩论简报的切入角度和结构建议写作。请输出：
标题：...

正文：...
"""


def _parse_image_slots(body: str) -> list[tuple[int, str]]:
    """从文章正文中解析插图标记 [插图：描述]，返回 [(位置在 body 中的字符偏移, 描述), ...]。"""
    import re
    pattern = re.compile(r'\[插图[：:](.+?)\]')
    return [(m.start(), m.group(1).strip()) for m in pattern.finditer(body)]


def _strip_image_slots(body: str) -> str:
    """从文章正文中移除插图标记，保留纯净正文。"""
    import re
    return re.sub(r'\[插图[：:].+?\]\n*', '\n', body).strip()


def _split_body_by_slots(body: str) -> list[dict]:
    """把正文按 [插图：...] 标记切片，返回段落序列供图文混排展示。

    返回 [{"type": "text", "text": "..."}, {"type": "slot", "slot_index": 0, "desc": "..."}] 交替序列。
    slot_index 从 0 递增，对应 ArticleImage.slot_index。
    """
    import re
    pattern = re.compile(r'\[插图[：:](.+?)\]')
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
    return result


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
    slot_marker = f"{_IMAGE_SLOT_MARK}{slot_desc}]"
    slot_pos = body.find(slot_marker)
    if slot_pos >= 0:
        context = body[max(0, slot_pos - 100):slot_pos + len(slot_marker) + 100]
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
