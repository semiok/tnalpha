"""①知识库 路由——样板模块：展示 CRUD + 权限守卫 + 组件复用。

权限：知识库写操作 = 定义者(owner, level 2)；浏览 = 所有登录角色。
"""
from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
from sqlmodel import Session, select

from app.core import auth
from app.core.db import get_session
from app.modules.knowledge.models import Brand, Campaign

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/")
def home(request: Request, session: Session = Depends(get_session)):
    brands = session.exec(select(Brand)).all()
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
    return templates.TemplateResponse(request, "knowledge/brand.html",
                                      {"brand": brand, "campaigns": campaigns})


@router.get("/styleguide")
def styleguide(request: Request):
    """组件样例页——开发参考，展示设计系统所有组件。"""
    return templates.TemplateResponse(request, "styleguide.html", {})
