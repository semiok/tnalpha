"""①知识库 路由——样板模块：CRUD + 文件上传 + AI 解析 + 数据池，全走 core 抽象。

权限（ARCHITECTURE §7）：写操作 = 定义者(owner, level 2)；浏览/下载 = 所有登录角色。
写操作一律 `auth.require_level(request, 2)` 服务端守卫，模板再按 level 显隐。
"""
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
from sqlmodel import Session, select

from app.core import auth, llm, storage
from app.core.db import get_session
from app.modules.knowledge.models import (
    Brand, BrandDoc, Campaign, CampaignDoc, PoolTopic,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _parse_date(value: str) -> date | None:
    value = (value or "").strip()
    return date.fromisoformat(value) if value else None


# ─────────────────────────────── 品牌 ───────────────────────────────

@router.get("/")
def home(request: Request, session: Session = Depends(get_session)):
    brands = session.exec(select(Brand).order_by(Brand.id)).all()
    return templates.TemplateResponse(request, "knowledge/home.html", {"brands": brands})


@router.post("/brands")
def create_brand(request: Request, name: str = Form(...),
                 session: Session = Depends(get_session)):
    auth.require_level(request, 2)
    brand = Brand(name=name)
    session.add(brand)
    session.commit()
    session.refresh(brand)
    # 每个品牌默认建一个"品牌日常"常驻 campaign（装日常选题）
    session.add(Campaign(brand_id=brand.id, name="品牌日常", is_default=True))
    session.commit()
    return RedirectResponse(f"/brands/{brand.id}", status_code=303)


@router.get("/brands/{brand_id}")
def brand_detail(brand_id: int, request: Request, session: Session = Depends(get_session)):
    brand = session.get(Brand, brand_id)
    if not brand:
        raise HTTPException(404, "品牌不存在")
    campaigns = session.exec(
        select(Campaign).where(Campaign.brand_id == brand_id)
        .order_by(Campaign.is_default.desc(), Campaign.id)).all()
    docs = session.exec(
        select(BrandDoc).where(BrandDoc.brand_id == brand_id)
        .order_by(BrandDoc.id.desc())).all()
    return templates.TemplateResponse(request, "knowledge/brand.html",
                                      {"brand": brand, "campaigns": campaigns, "docs": docs})


@router.post("/brands/{brand_id}/docs")
def upload_brand_doc(brand_id: int, request: Request,
                     file: UploadFile = File(...),
                     session: Session = Depends(get_session)):
    auth.require_level(request, 2)
    brand = session.get(Brand, brand_id)
    if not brand:
        raise HTTPException(404, "品牌不存在")
    path = storage.save_upload(file, subdir=f"brand/{brand_id}")
    session.add(BrandDoc(brand_id=brand_id, filename=file.filename, file_path=path))
    session.commit()
    return RedirectResponse(f"/brands/{brand_id}", status_code=303)


@router.post("/brands/{brand_id}/parse")
def parse_brand(brand_id: int, request: Request, session: Session = Depends(get_session)):
    """AI 解析品牌定义 → 存 brand_digest，HTMX 返回结果片段。"""
    auth.require_level(request, 2)
    brand = session.get(Brand, brand_id)
    if not brand:
        raise HTTPException(404, "品牌不存在")
    brand.brand_digest = llm.generate_text(brand.brand_prompt, task="brand_digest")
    session.add(brand)
    session.commit()
    return templates.TemplateResponse(
        request, "knowledge/_digest.html",
        {"digest": brand.brand_digest, "slot_id": "brand-digest"})


# ─────────────────────────────── Campaign ───────────────────────────────

@router.post("/campaigns")
def create_campaign(request: Request,
                    brand_id: int = Form(...), name: str = Form(...),
                    start_date: str = Form(""), end_date: str = Form(""),
                    session: Session = Depends(get_session)):
    auth.require_level(request, 2)
    if not session.get(Brand, brand_id):
        raise HTTPException(404, "品牌不存在")
    campaign = Campaign(brand_id=brand_id, name=name,
                        start_date=_parse_date(start_date),
                        end_date=_parse_date(end_date))
    session.add(campaign)
    session.commit()
    session.refresh(campaign)
    return RedirectResponse(f"/campaigns/{campaign.id}", status_code=303)


@router.get("/campaigns/{campaign_id}")
def campaign_detail(campaign_id: int, request: Request,
                    session: Session = Depends(get_session)):
    campaign = session.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404, "活动不存在")
    brand = session.get(Brand, campaign.brand_id)
    docs = session.exec(
        select(CampaignDoc).where(CampaignDoc.campaign_id == campaign_id)
        .order_by(CampaignDoc.id.desc())).all()
    return templates.TemplateResponse(request, "knowledge/campaign.html",
                                      {"campaign": campaign, "brand": brand, "docs": docs})


@router.post("/campaigns/{campaign_id}/docs")
def upload_campaign_doc(campaign_id: int, request: Request,
                        file: UploadFile = File(...), note: str = Form(""),
                        session: Session = Depends(get_session)):
    auth.require_level(request, 2)
    campaign = session.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404, "活动不存在")
    path = storage.save_upload(file, subdir=f"campaign/{campaign_id}")
    session.add(CampaignDoc(campaign_id=campaign_id, filename=file.filename,
                            file_path=path, note=note))
    session.commit()
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@router.post("/campaigns/{campaign_id}/parse")
def parse_campaign(campaign_id: int, request: Request,
                   session: Session = Depends(get_session)):
    """AI 解析活动资料 → 存 campaign_digest，HTMX 返回结果片段。"""
    auth.require_level(request, 2)
    campaign = session.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404, "活动不存在")
    docs = session.exec(
        select(CampaignDoc).where(CampaignDoc.campaign_id == campaign_id)).all()
    material = f"{campaign.name}\n" + "\n".join(f"- {d.filename}（{d.note}）" for d in docs)
    campaign.campaign_digest = llm.generate_text(material, task="campaign_digest")
    session.add(campaign)
    session.commit()
    return templates.TemplateResponse(
        request, "knowledge/_digest.html",
        {"digest": campaign.campaign_digest, "slot_id": "campaign-digest"})


@router.post("/campaigns/{campaign_id}/delete")
def delete_campaign(campaign_id: int, request: Request,
                    session: Session = Depends(get_session)):
    auth.require_level(request, 2)
    campaign = session.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404, "活动不存在")
    if campaign.is_default:
        raise HTTPException(400, "品牌日常不可删除")
    brand_id = campaign.brand_id
    for doc in session.exec(
            select(CampaignDoc).where(CampaignDoc.campaign_id == campaign_id)).all():
        session.delete(doc)
    session.delete(campaign)
    session.commit()
    # HTMX 调用走 HX-Redirect（客户端跳转，不 swap body）；普通请求走 303。
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": f"/brands/{brand_id}"})
    return RedirectResponse(f"/brands/{brand_id}", status_code=303)


# ─────────────────────────────── 数据池 ───────────────────────────────

@router.get("/pool")
def pool_list(request: Request, session: Session = Depends(get_session)):
    topics = session.exec(select(PoolTopic).order_by(PoolTopic.id.desc())).all()
    return templates.TemplateResponse(request, "knowledge/pool.html", {"topics": topics})


@router.post("/pool")
def create_pool_topic(request: Request,
                      title: str = Form(...), kind: str = Form("资料包"),
                      web_access: bool = Form(True), brand_tag: str = Form(""),
                      content: str = Form(""),
                      session: Session = Depends(get_session)):
    auth.require_level(request, 2)
    topic = PoolTopic(title=title, kind=kind, web_access=web_access,
                      source="upload", brand_tag=brand_tag or None, content=content)
    session.add(topic)
    session.commit()
    return RedirectResponse("/pool", status_code=303)


@router.get("/styleguide")
def styleguide(request: Request):
    """组件样例页——开发参考，展示设计系统所有组件。"""
    return templates.TemplateResponse(request, "styleguide.html", {})
