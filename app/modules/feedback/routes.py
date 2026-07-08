"""⑤数据反馈：发布复盘与经验包沉淀。"""
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
from starlette.requests import Request

from app.core import auth
from app.core.db import get_session
from app.modules.feedback import experience
from app.modules.feedback.models import EXPERIENCE_PLATFORMS, EXPERIENCE_TYPES, FeedbackExperience
from app.modules.knowledge.models import Brand, Campaign

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

FEEDBACK_TABS = [
    ("all", "全部"),
    ("待总结", "待总结"),
    ("已总结", "已总结"),
]


def _brand(session: Session) -> Brand | None:
    return session.exec(select(Brand).order_by(Brand.id)).first()


def _campaign_name(session: Session, brand_id: int) -> dict[int, str]:
    campaigns = session.exec(select(Campaign).where(Campaign.brand_id == brand_id).order_by(Campaign.id)).all()
    return {c.id: c.name for c in campaigns}


def _campaigns(session: Session, brand_id: int) -> list[Campaign]:
    return session.exec(select(Campaign).where(Campaign.brand_id == brand_id).order_by(Campaign.id)).all()


def _scope_from_query(raw: str | None) -> tuple[str, int | None]:
    if raw in (None, "", "all"):
        return "all", None
    if raw == "brand":
        return "brand", None
    try:
        return "campaign", int(raw)
    except (TypeError, ValueError):
        return "all", None


def _feedback_url(status: str = "all", campaign_id: str = "all") -> str:
    params = {}
    if status and status != "all":
        params["status"] = status
    if campaign_id and campaign_id != "all":
        params["campaign_id"] = campaign_id
    return "/feedback" + (f"?{urlencode(params)}" if params else "")


def _safe_feedback_redirect(raw: str | None, fallback: str = "/feedback") -> RedirectResponse:
    target = raw if raw and raw.startswith("/feedback") else fallback
    return RedirectResponse(target, status_code=303)


def _samples_in_scope(samples: list[experience.PublishedSample], scope: str,
                      campaign_id: int | None) -> list[experience.PublishedSample]:
    if scope == "brand":
        return [sample for sample in samples if sample.slot.campaign_id is None]
    if scope == "campaign":
        return [sample for sample in samples if sample.slot.campaign_id == campaign_id]
    return samples


def _samples_in_status(samples: list[experience.PublishedSample],
                       entries_by_slot: dict[int, list[FeedbackExperience]],
                       status: str) -> list[experience.PublishedSample]:
    if status in ("", "all"):
        return samples
    return [
        sample for sample in samples
        if experience.sample_status(entries_by_slot.get(sample.slot.id or 0)) == status
    ]


@router.get("/feedback")
def feedback_home(request: Request, status: str = "all", campaign_id: str = "all",
                  session: Session = Depends(get_session)):
    brand = _brand(session)
    active_status = status if status in {key for key, _label in FEEDBACK_TABS} else "all"
    if brand is None:
        return templates.TemplateResponse(request, "feedback/home.html", {
            "request": request,
            "brand": None,
            "campaigns": [],
            "samples": [],
            "entries": [],
            "entries_by_slot": {},
            "campaign_name": {},
            "experience_types": EXPERIENCE_TYPES,
            "experience_platforms": EXPERIENCE_PLATFORMS,
            "stats": {},
            "status_links": [],
            "scope_links": [],
            "active_status": active_status,
            "selected_campaign": "all",
        })
    campaigns = _campaigns(session, brand.id)
    campaign_name = {c.id: c.name for c in campaigns}
    all_samples = experience.published_samples(session, brand.id)
    slot_ids = [sample.slot.id for sample in all_samples if sample.slot.id is not None]
    entries_by_slot = experience.experiences_by_slot(session, slot_ids)
    entries = experience.experience_entries(session, brand.id)
    scope, cid = _scope_from_query(campaign_id)
    scoped_samples = _samples_in_scope(all_samples, scope, cid)
    samples = _samples_in_status(scoped_samples, entries_by_slot, active_status)
    summarized = sum(1 for sample in all_samples if experience.sample_status(entries_by_slot.get(sample.slot.id or 0)) == "已总结")
    stats = {
        "published": len(all_samples),
        "with_metrics": sum(1 for s in all_samples if s.metric is not None),
        "experiences": len(entries),
        "summarized": summarized,
        "pending": len(all_samples) - summarized,
        "high": sum(1 for s in all_samples if s.performance_level == "高表现"),
    }
    status_links = [
        (key, label, _feedback_url(key, campaign_id or "all"),
         len(_samples_in_status(scoped_samples, entries_by_slot, key)))
        for key, label in FEEDBACK_TABS
    ]
    scope_links = []
    scope_defs = [("all", "全部范围"), ("brand", "品牌常青")]
    scope_defs.extend((str(c.id), c.name) for c in campaigns)
    for scope_key, label in scope_defs:
        skind, scid = _scope_from_query(scope_key)
        in_scope = _samples_in_scope(all_samples, skind, scid)
        count = len(_samples_in_status(in_scope, entries_by_slot, active_status))
        scope_links.append((scope_key, label, _feedback_url(active_status, scope_key), count))
    return templates.TemplateResponse(request, "feedback/home.html", {
        "request": request,
        "brand": brand,
        "campaigns": campaigns,
        "samples": samples,
        "entries": entries,
        "entries_by_slot": entries_by_slot,
        "campaign_name": campaign_name,
        "experience_types": EXPERIENCE_TYPES,
        "experience_platforms": EXPERIENCE_PLATFORMS,
        "stats": stats,
        "status_links": status_links,
        "scope_links": scope_links,
        "active_status": active_status,
        "selected_campaign": campaign_id or "all",
    })


@router.post("/feedback/experiences/from-slot")
def create_from_slot(request: Request, slot_id: int = Form(...),
                     scope: str = Form("campaign"), platform: str = Form(""),
                     return_to: str = Form("/feedback?status=已总结"),
                     session: Session = Depends(get_session)):
    auth.require_level(request, 1)
    try:
        experience.create_experience_pair_from_slot(session, slot_id, scope, platform)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    separator = "&" if "?" in return_to else "?"
    target = f"{return_to}{separator}open={slot_id}#sample-{slot_id}"
    return _safe_feedback_redirect(target)


@router.post("/feedback/experiences/batch")
def create_batch(request: Request, slot_id: list[int] = Form([]),
                 scope: str = Form("campaign"),
                 return_to: str = Form("/feedback?status=已总结"),
                 session: Session = Depends(get_session)):
    auth.require_level(request, 1)
    if not slot_id:
        return _safe_feedback_redirect(return_to)
    try:
        experience.create_experience_pairs_from_slots(session, slot_id, scope=scope)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _safe_feedback_redirect(return_to)


@router.post("/feedback/experiences/slot/{slot_id}/update")
def update_slot_experience(slot_id: int, request: Request,
                           topic_title: str = Form(""), topic_summary: str = Form(""),
                           topic_positive_notes: str = Form(""), topic_negative_notes: str = Form(""),
                           topic_action_advice: str = Form(""),
                           writing_title: str = Form(""), writing_summary: str = Form(""),
                           writing_positive_notes: str = Form(""), writing_negative_notes: str = Form(""),
                           writing_action_advice: str = Form(""),
                           return_to: str = Form("/feedback"),
                           session: Session = Depends(get_session)):
    auth.require_level(request, 1)
    entries = session.exec(
        select(FeedbackExperience).where(
            FeedbackExperience.source_slot_id == slot_id,
            FeedbackExperience.is_active == True,
        )
    ).all()
    by_type = {entry.experience_type: entry for entry in entries}
    field_sets = {
        "选题经验": {
            "title": topic_title.strip() or "选题经验",
            "summary": topic_summary.strip(),
            "positive_notes": topic_positive_notes.strip(),
            "negative_notes": topic_negative_notes.strip(),
            "action_advice": topic_action_advice.strip(),
        },
        "写作经验": {
            "title": writing_title.strip() or "写作经验",
            "summary": writing_summary.strip(),
            "positive_notes": writing_positive_notes.strip(),
            "negative_notes": writing_negative_notes.strip(),
            "action_advice": writing_action_advice.strip(),
        },
    }
    for experience_type, fields in field_sets.items():
        entry = by_type.get(experience_type)
        if entry is None or entry.id is None:
            continue
        experience.update_experience(session, entry.id, **fields)
    separator = "&" if "?" in return_to else "?"
    return _safe_feedback_redirect(f"{return_to}{separator}open={slot_id}#sample-{slot_id}")


@router.post("/feedback/experiences")
def create_manual(request: Request, brand_id: int = Form(...), campaign_id: str = Form(""),
                  platform: str = Form("通用"), experience_type: str = Form("选题经验"),
                  title: str = Form(""), summary: str = Form(""),
                  positive_notes: str = Form(""), negative_notes: str = Form(""),
                  action_advice: str = Form(""), session: Session = Depends(get_session)):
    auth.require_level(request, 1)
    cid = int(campaign_id) if campaign_id.strip() else None
    entry = FeedbackExperience(
        brand_id=brand_id,
        campaign_id=cid,
        platform=platform or "通用",
        experience_type=experience_type,
        title=title.strip() or "手写经验",
        summary=summary.strip(),
        positive_notes=positive_notes.strip(),
        negative_notes=negative_notes.strip(),
        action_advice=action_advice.strip(),
    )
    session.add(entry)
    session.commit()
    session.refresh(entry)
    return RedirectResponse(f"/feedback#experience-{entry.id}", status_code=303)


@router.post("/feedback/experiences/{entry_id}/update")
def update_entry(entry_id: int, request: Request,
                 platform: str = Form("通用"), experience_type: str = Form("选题经验"),
                 title: str = Form(""), summary: str = Form(""),
                 positive_notes: str = Form(""), negative_notes: str = Form(""),
                 action_advice: str = Form(""), session: Session = Depends(get_session)):
    auth.require_level(request, 1)
    try:
        experience.update_experience(
            session,
            entry_id,
            platform=platform or "通用",
            experience_type=experience_type,
            title=title.strip() or "未命名经验",
            summary=summary.strip(),
            positive_notes=positive_notes.strip(),
            negative_notes=negative_notes.strip(),
            action_advice=action_advice.strip(),
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return RedirectResponse(f"/feedback#experience-{entry_id}", status_code=303)


@router.post("/feedback/experiences/{entry_id}/delete")
def delete_entry(entry_id: int, request: Request, session: Session = Depends(get_session)):
    auth.require_level(request, 1)
    experience.deactivate_experience(session, entry_id)
    return RedirectResponse("/feedback", status_code=303)
