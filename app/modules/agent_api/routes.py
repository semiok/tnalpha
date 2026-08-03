"""Versioned machine API for OpenLoomi's TN-Alpha digital employee."""
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import hmac
import json
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field as PydanticField
from sqlmodel import Session, select

from app import __version__
from app.core import auth, config
from app.core.db import get_session
from app.modules.agent_api.models import AgentAuditLog
from app.modules.feedback.models import FeedbackExperience
from app.modules.knowledge.models import Brand, BrandDoc, Campaign, CampaignDoc
from app.modules.schedule.models import ScheduleSlot
from app.modules.topic.models import Topic
from app.modules.writing.models import Article, Style

router = APIRouter(prefix="/api/v1", tags=["agent-api"])


@dataclass(frozen=True)
class AgentPrincipal:
    actor_id: str
    org_id: str
    role: str = "admin0"


class ManualTopicInput(BaseModel):
    brand_id: int
    campaign_id: int | None = None
    title: str = PydanticField(min_length=1, max_length=300)
    outline: str = PydanticField(default="", max_length=5000)
    angle: str = PydanticField(default="", max_length=1000)
    audience: str = PydanticField(default="", max_length=1000)
    content_type: str = PydanticField(default="", max_length=200)
    timeliness: str = PydanticField(default="", max_length=200)
    materials: str = PydanticField(default="", max_length=5000)
    image_hint: str = PydanticField(default="", max_length=2000)
    publish_window: str = PydanticField(default="", max_length=1000)


def require_agent(request: Request) -> AgentPrincipal:
    configured = config.AGENT_API_TOKEN.strip()
    if not configured:
        raise HTTPException(status_code=503, detail="Agent API is not configured")
    authorization = request.headers.get("authorization", "")
    scheme, _, supplied = authorization.partition(" ")
    if scheme.lower() != "bearer" or not supplied or not hmac.compare_digest(supplied, configured):
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")
    actor_id = request.headers.get("x-tnalpha-actor", "openloomi-admin").strip()[:200]
    org_id = request.headers.get("x-tnalpha-org", config.AGENT_API_ORG_ID).strip()[:200]
    return AgentPrincipal(actor_id=actor_id or "openloomi-admin", org_id=org_id or config.AGENT_API_ORG_ID)


def _status_counts(rows) -> dict[str, int]:
    return dict(sorted(Counter(row.status for row in rows).items()))


def _topic_dict(topic: Topic) -> dict:
    return {
        "id": topic.id,
        "brand_id": topic.brand_id,
        "campaign_id": topic.campaign_id,
        "title": topic.title,
        "outline": topic.outline,
        "status": topic.status,
        "source": topic.source,
        "created_at": topic.created_at,
    }


@router.get("/health")
def api_health():
    return {"status": "ok", "service": "tnalpha-agent-api", "version": __version__}


@router.get("/me")
def me(principal: AgentPrincipal = Depends(require_agent)):
    return {
        "actor_id": principal.actor_id,
        "org_id": principal.org_id,
        "role": principal.role,
        "role_label": auth.label_of(principal.role),
        "access": "highest",
    }


@router.get("/capabilities")
def capabilities(principal: AgentPrincipal = Depends(require_agent)):
    return {
        "role": principal.role,
        "modules": [
            {"key": key, "label": value["label"], "read": True, "write": True}
            for key, value in auth.MODULES.items()
        ],
        "implemented_actions": [
            "brands.list",
            "campaigns.list",
            "overview.read",
            "topics.list",
            "topics.create_manual",
            "audit.list",
        ],
        "restricted_actions": ["publish.execute", "resource.delete", "model.manage", "user.manage"],
    }


@router.get("/brands")
def list_brands(
    principal: AgentPrincipal = Depends(require_agent),
    session: Session = Depends(get_session),
):
    del principal
    brands = session.exec(select(Brand).order_by(Brand.id)).all()
    result = []
    for brand in brands:
        campaigns = session.exec(select(Campaign).where(Campaign.brand_id == brand.id)).all()
        docs = session.exec(select(BrandDoc).where(BrandDoc.brand_id == brand.id)).all()
        styles = session.exec(select(Style).where(Style.brand_id == brand.id)).all()
        result.append({
            "id": brand.id,
            "name": brand.name,
            "analysis_status": brand.analysis_status,
            "campaign_count": len(campaigns),
            "document_count": len(docs),
            "style_count": len(styles),
            "created_at": brand.created_at,
        })
    return {"items": result, "count": len(result)}


@router.get("/brands/{brand_id}/campaigns")
def list_campaigns(
    brand_id: int,
    principal: AgentPrincipal = Depends(require_agent),
    session: Session = Depends(get_session),
):
    del principal
    brand = session.get(Brand, brand_id)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    campaigns = session.exec(
        select(Campaign).where(Campaign.brand_id == brand_id).order_by(Campaign.id)
    ).all()
    items = []
    for campaign in campaigns:
        doc_count = len(session.exec(
            select(CampaignDoc).where(CampaignDoc.campaign_id == campaign.id)
        ).all())
        items.append({
            "id": campaign.id,
            "brand_id": campaign.brand_id,
            "name": campaign.name,
            "is_default": campaign.is_default,
            "start_date": campaign.start_date,
            "end_date": campaign.end_date,
            "analysis_status": campaign.analysis_status,
            "document_count": doc_count,
        })
    return {"brand": {"id": brand.id, "name": brand.name}, "items": items, "count": len(items)}


@router.get("/brands/{brand_id}/overview")
def brand_overview(
    brand_id: int,
    principal: AgentPrincipal = Depends(require_agent),
    session: Session = Depends(get_session),
):
    del principal
    brand = session.get(Brand, brand_id)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    campaigns = session.exec(select(Campaign).where(Campaign.brand_id == brand_id)).all()
    topics = session.exec(select(Topic).where(Topic.brand_id == brand_id)).all()
    topic_ids = [topic.id for topic in topics if topic.id is not None]
    articles = session.exec(select(Article).where(Article.topic_id.in_(topic_ids))).all() if topic_ids else []
    slots = session.exec(select(ScheduleSlot).where(ScheduleSlot.brand_id == brand_id)).all()
    experiences = session.exec(
        select(FeedbackExperience).where(
            FeedbackExperience.brand_id == brand_id,
            FeedbackExperience.is_active == True,  # noqa: E712
        )
    ).all()
    return {
        "brand": {"id": brand.id, "name": brand.name, "analysis_status": brand.analysis_status},
        "campaigns": len(campaigns),
        "topics": {"total": len(topics), "by_status": _status_counts(topics)},
        "articles": {"total": len(articles), "by_status": _status_counts(articles)},
        "schedule": {"total": len(slots), "by_status": _status_counts(slots)},
        "active_experiences": len(experiences),
    }


@router.get("/topics")
def list_topics(
    brand_id: int | None = None,
    campaign_id: int | None = None,
    status: str | None = None,
    limit: int = 50,
    principal: AgentPrincipal = Depends(require_agent),
    session: Session = Depends(get_session),
):
    del principal
    statement = select(Topic)
    if brand_id is not None:
        statement = statement.where(Topic.brand_id == brand_id)
    if campaign_id is not None:
        statement = statement.where(Topic.campaign_id == campaign_id)
    if status:
        statement = statement.where(Topic.status == status)
    rows = session.exec(statement.order_by(Topic.id.desc()).limit(max(1, min(limit, 200)))).all()
    return {"items": [_topic_dict(row) for row in rows], "count": len(rows)}


@router.post("/topics/manual", status_code=201)
def create_manual_topic(
    payload: ManualTopicInput,
    request: Request,
    principal: AgentPrincipal = Depends(require_agent),
    session: Session = Depends(get_session),
):
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="Title cannot be blank")
    brand = session.get(Brand, payload.brand_id)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    if payload.campaign_id is not None:
        campaign = session.get(Campaign, payload.campaign_id)
        if not campaign or campaign.brand_id != payload.brand_id:
            raise HTTPException(status_code=400, detail="Campaign does not belong to brand")
    duplicate = session.exec(
        select(Topic).where(
            Topic.brand_id == payload.brand_id,
            Topic.campaign_id == payload.campaign_id,
            Topic.title == title,
        )
    ).first()
    if duplicate:
        raise HTTPException(status_code=409, detail={"message": "Topic already exists", "topic_id": duplicate.id})

    topic = Topic(
        brand_id=payload.brand_id,
        campaign_id=payload.campaign_id,
        title=title,
        outline=payload.outline.strip(),
        angle=payload.angle.strip(),
        audience=payload.audience.strip(),
        content_type=payload.content_type.strip(),
        timeliness=payload.timeliness.strip(),
        materials=payload.materials.strip(),
        image_hint=payload.image_hint.strip(),
        publish_window=payload.publish_window.strip(),
        status="候选",
        source="added",
    )
    session.add(topic)
    session.flush()

    request_id = request.headers.get("x-request-id", "").strip()[:200] or str(uuid4())
    audit = AgentAuditLog(
        request_id=request_id,
        actor_id=principal.actor_id,
        org_id=principal.org_id,
        role=principal.role,
        action="topics.create_manual",
        resource_type="topic",
        resource_id=str(topic.id),
        brand_id=payload.brand_id,
        campaign_id=payload.campaign_id,
        payload_json=json.dumps({"title": title}, ensure_ascii=False),
        result="success",
    )
    session.add(audit)
    session.commit()
    session.refresh(topic)
    return {"item": _topic_dict(topic), "audit": {"request_id": request_id, "actor_id": principal.actor_id}}


@router.get("/audit")
def list_audit(
    limit: int = 50,
    principal: AgentPrincipal = Depends(require_agent),
    session: Session = Depends(get_session),
):
    del principal
    rows = session.exec(
        select(AgentAuditLog).order_by(AgentAuditLog.id.desc()).limit(max(1, min(limit, 200)))
    ).all()
    return {
        "items": [
            {
                "id": row.id,
                "request_id": row.request_id,
                "actor_id": row.actor_id,
                "org_id": row.org_id,
                "role": row.role,
                "action": row.action,
                "resource_type": row.resource_type,
                "resource_id": row.resource_id,
                "brand_id": row.brand_id,
                "campaign_id": row.campaign_id,
                "result": row.result,
                "created_at": row.created_at,
            }
            for row in rows
        ],
        "count": len(rows),
        "generated_at": datetime.now(),
    }
