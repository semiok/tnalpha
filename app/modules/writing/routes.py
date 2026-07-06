"""③写作引擎：采纳选题 → 风格注入 → 文章 → 配图。

边界：读② Topic(status='采纳')，写③ Article/Style；不回写 Topic.status。
"""
from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
from starlette.requests import Request

from app.core import auth, llm, sources
from app.core.db import get_session
from app.modules.knowledge.models import Brand, Campaign
from app.modules.topic.contract import KnowledgeContext
from app.modules.topic.models import Topic
from app.modules.writing.models import Article, ArticleImage, Style

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


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


@router.get("/writing")
def writing_home(request: Request, session: Session = Depends(get_session)):
    brand = _first_brand(session)
    topics: list[Topic] = []
    campaigns: list[Campaign] = []
    articles: dict[int, Article] = {}
    styles: dict[int, list[Style]] = {}
    if brand is not None:
        campaigns = session.exec(
            select(Campaign).where(Campaign.brand_id == brand.id).order_by(Campaign.id)
        ).all()
        topics = session.exec(
            select(Topic).where(Topic.brand_id == brand.id, Topic.status == "采纳").order_by(Topic.created_at.desc())
        ).all()
        topic_ids = [t.id for t in topics if t.id is not None]
        if topic_ids:
            for a in session.exec(select(Article).where(Article.topic_id.in_(topic_ids))).all():
                cur = articles.get(a.topic_id)
                if cur is None or a.updated_at >= cur.updated_at:
                    articles[a.topic_id] = a
        for c in campaigns:
            styles[c.id] = session.exec(
                select(Style).where(Style.campaign_id == c.id).order_by(Style.is_default.desc(), Style.id)
            ).all()
    cmap = {c.id: c.name for c in campaigns}
    return templates.TemplateResponse(request, "writing/home.html", {
        "brand": brand,
        "topics": topics,
        "campaigns": campaigns,
        "cmap": cmap,
        "articles": articles,
        "styles": styles,
        "catalog": sources.catalog(),
    })


@router.post("/writing/styles/campaign/{campaign_id}/capture")
def capture_styles(campaign_id: int, request: Request, query: str = Form(""),
                   source: list[str] = Form([]), count: int = Form(5),
                   session: Session = Depends(get_session)):
    auth.require_level(request, 1)
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(404, "活动不存在")
    names = [s for s in source if s in sources.available()] or ["stub"]
    q = query.strip() or f"{campaign.name} 写作风格"
    hits = sources.gather(names, q, per_source=max(1, min(count, 5))) or sources.search("stub", q)
    existing_default = _default_style(session, campaign_id) is not None
    created = 0
    for hit in hits[:max(1, min(count, 5))]:
        style = _style_from_hit(campaign_id, hit, is_default=(not existing_default and created == 0))
        session.add(style)
        created += 1
    session.commit()
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


@router.post("/writing/topics/{topic_id}/generate")
def generate_article(topic_id: int, request: Request, session: Session = Depends(get_session)):
    auth.require_level(request, 1)
    topic = session.get(Topic, topic_id)
    if topic is None:
        raise HTTPException(404, "选题不存在")
    if topic.status != "采纳":
        raise HTTPException(400, "只有已采纳选题可以进入写作")
    ctx = KnowledgeContext.load(session, topic.brand_id, topic.campaign_id)
    style = _default_style(session, topic.campaign_id)
    prompt = _article_prompt(topic, ctx, style)
    body = llm.generate_text(prompt, task="writing_article", module="writing")
    image_prompt = _image_prompt(topic, ctx, style, body)
    image_url = llm.generate_image(image_prompt, module="writing")
    article = session.exec(select(Article).where(Article.topic_id == topic.id)).first()
    if article is None:
        article = Article(topic_id=topic.id, campaign_id=topic.campaign_id, title=_article_title(body, topic.title))
    article.style_id = style.id if style else None
    article.title = _article_title(body, topic.title)
    article.body = body
    article.image_prompt = image_prompt
    article.image_url = image_url
    article.status = "图文完成"
    session.add(article)
    session.commit()
    session.refresh(article)
    session.add(ArticleImage(article_id=article.id, prompt=image_prompt, image_url=image_url))
    session.commit()
    return RedirectResponse("/writing", status_code=303)


def _article_prompt(topic: Topic, ctx: KnowledgeContext, style: Style | None) -> str:
    style_text = style.summary if style else "无默认风格，使用品牌内容要求。"
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

请输出：
标题：...

正文：...
"""


def _image_prompt(topic: Topic, ctx: KnowledgeContext, style: Style | None, body: str) -> str:
    style_text = style.summary if style else ""
    return (
        f"为文章《{topic.title}》生成配图。配图方向：{topic.image_hint}。"
        f"品牌视觉风格：{ctx.style_digest}。默认写作风格参考：{style_text}。"
        f"文章摘要：{body[:500]}"
    )
