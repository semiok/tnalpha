"""②选题库 路由——读①知识库(KnowledgeContext) 生成选题 → 候选 → 采纳。

权限（ARCHITECTURE §7）：生成/采纳/删除 = 选题者(editor, level 1)+；浏览 = 所有登录角色。
"""
from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
from starlette.requests import Request

from app.core import auth, sources
from app.core.db import get_session
from app.modules.knowledge.models import Brand, Campaign
from app.modules.topic.generate import generate_topics
from app.modules.topic.models import TOPIC_STATUSES, Topic

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# 分类 tab：(key, 显示名, 匹配的 status 集合)。None=全部；已创作/已发布等③写作引擎产出后才有数据。
TABS = [
    ("all", "全部", None),
    ("候选", "候选", ("候选",)),
    ("采纳", "已采纳", ("采纳",)),
    ("已创作", "已创作", ("写作中", "图文完成")),
    ("已发布", "已发布", ("已发布",)),
]


def _brand_or_404(session: Session) -> Brand:
    brand = session.exec(select(Brand).order_by(Brand.id)).first()
    if brand is None:
        raise HTTPException(404, "还没有品牌，先去①知识库建/解析品牌")
    return brand


@router.get("/topics")
def topics_home(request: Request, status: str = "", session: Session = Depends(get_session)):
    brand = session.exec(select(Brand).order_by(Brand.id)).first()
    campaigns, topics, cmap, tab_counts = [], [], {}, {}
    active = status or "all"
    if brand is not None:
        campaigns = session.exec(
            select(Campaign).where(Campaign.brand_id == brand.id).order_by(Campaign.id)).all()
        all_topics = session.exec(
            select(Topic).where(Topic.brand_id == brand.id).order_by(Topic.created_at.desc())).all()
        for key, _label, sts in TABS:   # 每个 tab 的计数
            tab_counts[key] = len(all_topics) if sts is None else sum(t.status in sts for t in all_topics)
        cur = next((t for t in TABS if t[0] == active), TABS[0])
        topics = all_topics if cur[2] is None else [t for t in all_topics if t.status in cur[2]]
        cmap = {c.id: c.name for c in campaigns}
    return templates.TemplateResponse(request, "topic/home.html", {
        "brand": brand, "campaigns": campaigns, "topics": topics, "cmap": cmap,
        "statuses": TOPIC_STATUSES, "catalog": sources.catalog(),
        "tabs": TABS, "tab_counts": tab_counts, "active_tab": active})


@router.post("/topics/generate")
def generate(request: Request, campaign_id: str = Form(""), count: int = Form(5),
             source: list[str] = Form([]), hot_query: str = Form(""),
             session: Session = Depends(get_session)):
    """生成候选选题。campaign_id 空=品牌常青；source=勾选的搜索源；hot_query=热点关键词。"""
    auth.require_level(request, 1)
    brand = _brand_or_404(session)
    cid = int(campaign_id) if campaign_id.strip() else None
    if cid is not None and session.get(Campaign, cid) is None:
        raise HTTPException(404, "活动不存在")
    valid = [s for s in source if s in sources.available()]   # 只认注册过的源
    generate_topics(session, brand.id, cid, count=min(max(count, 1), 10),
                    sources_used=valid, hot_query=hot_query)
    return RedirectResponse("/topics?status=候选", status_code=303)


@router.post("/topics/{topic_id}/adopt")
def adopt(topic_id: int, request: Request, session: Session = Depends(get_session)):
    """采纳候选：候选 → 采纳(待写作)。"""
    auth.require_level(request, 1)
    t = session.get(Topic, topic_id)
    if not t:
        raise HTTPException(404, "选题不存在")
    if t.status == "候选":
        t.status = "采纳"
        session.add(t)
        session.commit()
    return RedirectResponse("/topics", status_code=303)


@router.post("/topics/{topic_id}/unadopt")
def unadopt(topic_id: int, request: Request, session: Session = Depends(get_session)):
    """取消采纳：采纳 → 候选。仅②内部状态回退；③写作引擎已接手的选题不应回退（待③接入后加守卫）。"""
    auth.require_level(request, 1)
    t = session.get(Topic, topic_id)
    if not t:
        raise HTTPException(404, "选题不存在")
    if t.status == "采纳":
        t.status = "候选"
        session.add(t)
        session.commit()
    return RedirectResponse("/topics", status_code=303)


@router.post("/topics/{topic_id}/delete")
def delete(topic_id: int, request: Request, session: Session = Depends(get_session)):
    auth.require_level(request, 1)
    t = session.get(Topic, topic_id)
    if t:
        session.delete(t)
        session.commit()
    return RedirectResponse("/topics", status_code=303)
