"""④排期版：待审核文章 → 发布周排期 → 发布回填。"""
from datetime import date, datetime

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
from starlette.requests import Request

from app.core import auth
from app.core.db import get_session
from app.modules.knowledge.models import Campaign
from app.modules.schedule import schedule
from app.modules.schedule.models import ScheduleSlot, ScheduleWeek
from app.modules.topic.models import Topic
from app.modules.writing.models import Article

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _scope_from_query(raw: str | None) -> tuple[str, int | None]:
    if raw in (None, "", "all"):
        return "all", None
    if raw == "brand":
        return "brand", None
    try:
        return "campaign", int(raw)
    except (TypeError, ValueError):
        return "all", None


def _scope_from_form(raw: str | None) -> int | None:
    if raw in (None, "", "brand"):
        return None
    return int(raw)


def _redirect(campaign_id: str | None = None) -> RedirectResponse:
    suffix = f"?campaign_id={campaign_id}" if campaign_id else ""
    return RedirectResponse(f"/schedule{suffix}", status_code=303)


def _campaigns(session: Session, brand_id: int) -> list[Campaign]:
    return session.exec(select(Campaign).where(Campaign.brand_id == brand_id).order_by(Campaign.id)).all()


def _ctx(request: Request, session: Session, campaign_id_raw: str | None = None) -> dict:
    brand = schedule.first_brand(session)
    if brand is None:
        return {"request": request, "brand": None, "campaigns": [], "weeks": [],
                "week_slots": {}, "articles": [], "slot_articles": {},
                "article_topics": {}, "campaign_name": {},
                "selected_campaign": "all", "level": getattr(request.state, "level", 0)}
    selected_scope, selected_campaign_id = _scope_from_query(campaign_id_raw)
    campaigns = _campaigns(session, brand.id)
    campaign_name = {c.id: c.name for c in campaigns}
    if selected_scope == "all":
        weeks = schedule.all_weeks(session, brand.id)
        articles = schedule.all_schedulable_articles(session, brand.id)
    else:
        weeks = schedule.weeks(session, brand.id, selected_campaign_id)
        articles = schedule.schedulable_articles(session, brand.id, selected_campaign_id)
    week_slots = {w.id: schedule.week_slots(session, w.id) for w in weeks if w.id is not None}
    topic_ids = {a.topic_id for a in articles}
    slot_article_ids: set[int] = set()
    for slots in week_slots.values():
        topic_ids.update(slot.topic_id for slot in slots)
        slot_article_ids.update(slot.article_id for slot in slots)
    article_topics = {
        t.id: t for t in session.exec(select(Topic).where(Topic.id.in_(topic_ids))).all()
    } if topic_ids else {}
    slot_articles = {
        a.id: a for a in session.exec(select(Article).where(Article.id.in_(slot_article_ids))).all()
    } if slot_article_ids else {}
    return {
        "request": request,
        "brand": brand,
        "campaigns": campaigns,
        "weeks": weeks,
        "week_slots": week_slots,
        "articles": articles,
        "slot_articles": slot_articles,
        "article_topics": article_topics,
        "campaign_name": campaign_name,
        "selected_campaign": campaign_id_raw or "all",
        "level": getattr(request.state, "level", 0),
    }


@router.get("/schedule")
def schedule_home(request: Request, campaign_id: str = "all",
                  session: Session = Depends(get_session)):
    return templates.TemplateResponse(request, "schedule/home.html", _ctx(request, session, campaign_id))


@router.post("/schedule/weeks/add")
def add_week(request: Request, campaign_id: str = Form("brand"),
             session: Session = Depends(get_session)):
    auth.require_level(request, 1)
    brand = schedule.first_brand(session)
    if brand is None:
        raise HTTPException(404, "还没有品牌")
    cid = _scope_from_form(campaign_id)
    if cid is not None and session.get(Campaign, cid) is None:
        raise HTTPException(404, "活动不存在")
    schedule.add_week(session, brand.id, cid)
    return _redirect(campaign_id if campaign_id else "brand")


@router.post("/schedule/recommend")
def recommend_schedule(request: Request, campaign_id: str = Form("all"),
                       session: Session = Depends(get_session)):
    auth.require_level(request, 1)
    brand = schedule.first_brand(session)
    if brand is None:
        raise HTTPException(404, "还没有品牌")
    scope, cid = _scope_from_query(campaign_id)
    if scope == "all":
        schedule.recommend_slots(session, brand.id, None)
        for campaign in _campaigns(session, brand.id):
            schedule.recommend_slots(session, brand.id, campaign.id)
    else:
        schedule.recommend_slots(session, brand.id, cid)
    return _redirect(campaign_id)


@router.post("/schedule/weeks/{week_id}/delete")
def delete_week(week_id: int, request: Request, session: Session = Depends(get_session)):
    auth.require_level(request, 1)
    try:
        schedule.delete_week(session, week_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _redirect()


@router.post("/schedule/slots/add")
def add_slot(request: Request, week_id: int = Form(...), article_id: int = Form(...),
             publish_date: date | None = Form(None), publish_time: str = Form(""),
             platform: str = Form(""), notes: str = Form(""),
             session: Session = Depends(get_session)):
    auth.require_level(request, 1)
    try:
        schedule.add_slot(session, week_id, article_id, publish_date, publish_time, platform, notes)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _redirect()


@router.post("/schedule/slots/{slot_id}/move")
def move_slot(slot_id: int, request: Request, publish_date: date = Form(...),
              publish_time: str = Form(""), session: Session = Depends(get_session)):
    auth.require_level(request, 1)
    try:
        schedule.move_slot(session, slot_id, publish_date, publish_time)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _redirect()


@router.post("/schedule/slots/{slot_id}/remove")
def remove_slot(slot_id: int, request: Request, session: Session = Depends(get_session)):
    auth.require_level(request, 1)
    try:
        schedule.remove_slot(session, slot_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _redirect()


@router.post("/schedule/slots/{slot_id}/publish")
def publish_slot(slot_id: int, request: Request, platform: str = Form(""),
                 published_url: str = Form(""), published_at: str = Form(""),
                 session: Session = Depends(get_session)):
    auth.require_level(request, 0)
    parsed_at = None
    if published_at.strip():
        try:
            parsed_at = datetime.fromisoformat(published_at.strip())
        except ValueError as exc:
            raise HTTPException(400, "发布时间格式不正确") from exc
    try:
        schedule.publish_slot(session, slot_id, platform, published_url, parsed_at)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _redirect()


@router.post("/schedule/slots/{slot_id}/unpublish")
def unpublish_slot(slot_id: int, request: Request, session: Session = Depends(get_session)):
    auth.require_level(request, 0)
    try:
        schedule.unpublish_slot(session, slot_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _redirect()


@router.get("/schedule/articles/{article_id}/preview")
def article_preview(article_id: int, request: Request, session: Session = Depends(get_session)):
    article = session.get(Article, article_id)
    if article is None:
        raise HTTPException(404, "文章不存在")
    topic = session.get(Topic, article.topic_id)
    return templates.TemplateResponse(request, "schedule/_article_preview.html", {
        "request": request,
        "article": article,
        "topic": topic,
    })
