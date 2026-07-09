"""②选题库 路由——读①知识库(KnowledgeContext) 生成选题 → 候选 → 采纳。

权限（ARCHITECTURE §7）：生成/采纳/删除 = 选题者(editor, level 1)+；浏览 = 所有登录角色。
"""
from datetime import datetime

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select
from starlette.requests import Request

from app.core import auth, sources
from app.core.db import get_session
from app.core.llm.errors import ModelRateLimited
from app.core.templates import create_templates
from app.modules.knowledge.models import Brand, Campaign
from app.modules.topic.generate import create_manual_topics, generate_topics
from app.modules.topic.models import TOPIC_STATUSES, Topic

router = APIRouter()
templates = create_templates()

# 分类 tab：(key, 显示名, 匹配的 status 集合)。None=全部；已创作/已发布等③写作引擎产出后才有数据。
TABS = [
    ("all", "全部", None),
    ("候选", "候选", ("候选",)),
    ("采纳", "已采纳", ("采纳",)),
    ("已创作", "已创作", ("写作中", "图文完成")),
    ("已发布", "已发布", ("已发布",)),
    ("回收站", "回收站", ("回收站",)),
]


def _topic_url(status: str = "all", scope: str = "all") -> str:
    params = {}
    if status and status != "all":
        params["status"] = status
    if scope and scope != "all":
        params["scope"] = scope
    query = urlencode(params)
    return f"/topics?{query}" if query else "/topics"


def _scope_from_query(raw: str | None) -> tuple[str, int | None]:
    if raw in (None, "", "all"):
        return "all", None
    if raw == "brand":
        return "brand", None
    if raw.startswith("campaign:"):
        raw = raw.split(":", 1)[1]
    try:
        return "campaign", int(raw)
    except (TypeError, ValueError):
        return "all", None


def _topic_in_scope(topic: Topic, scope: str, campaign_id: int | None) -> bool:
    if scope == "brand":
        return topic.campaign_id is None
    if scope == "campaign":
        return topic.campaign_id == campaign_id
    return True


def _topics_in_status(topics: list[Topic], statuses: tuple[str, ...] | None) -> list[Topic]:
    if statuses is None:
        return [t for t in topics if t.status != "回收站"]
    return [t for t in topics if t.status in statuses]


def _brand_or_404(session: Session) -> Brand:
    brand = session.exec(select(Brand).order_by(Brand.id)).first()
    if brand is None:
        raise HTTPException(404, "还没有品牌，先去①知识库建/解析品牌")
    return brand


@router.get("/topics")
def topics_home(request: Request, status: str = "", scope: str = "all",
                error: str = "", modal_error: str = "",
                session: Session = Depends(get_session)):
    brand = session.exec(select(Brand).order_by(Brand.id)).first()
    campaigns, topics, cmap, tab_counts, scope_counts = [], [], {}, {}, {}
    active = status or "all"
    active_scope = scope or "all"
    if brand is not None:
        campaigns = session.exec(
            select(Campaign).where(Campaign.brand_id == brand.id).order_by(Campaign.id)).all()
        all_topics = session.exec(
            select(Topic).where(Topic.brand_id == brand.id).order_by(Topic.created_at.desc())).all()
        scope_kind, scope_campaign_id = _scope_from_query(active_scope)
        active_scope = (
            "brand" if scope_kind == "brand"
            else f"campaign:{scope_campaign_id}" if scope_kind == "campaign" and scope_campaign_id is not None
            else "all"
        )
        scoped_topics = [t for t in all_topics if _topic_in_scope(t, scope_kind, scope_campaign_id)]
        for key, _label, sts in TABS:   # 每个 tab 的计数
            tab_counts[key] = len(_topics_in_status(scoped_topics, sts))
        cur = next((t for t in TABS if t[0] == active), TABS[0])
        topics = _topics_in_status(scoped_topics, cur[2])
        for scope_key, _label in [("all", "全部范围"), ("brand", "品牌常青")]:
            st = cur[2]
            in_scope = [t for t in all_topics if _topic_in_scope(t, *_scope_from_query(scope_key))]
            scope_counts[scope_key] = len(_topics_in_status(in_scope, st))
        for c in campaigns:
            scope_key = f"campaign:{c.id}"
            st = cur[2]
            in_scope = [t for t in all_topics if _topic_in_scope(t, *_scope_from_query(scope_key))]
            scope_counts[scope_key] = len(_topics_in_status(in_scope, st))
        cmap = {c.id: c.name for c in campaigns}
    tab_links = [
        (key, label, _topic_url(key, active_scope), tab_counts.get(key, 0))
        for key, label, _sts in TABS
    ]
    scope_links = [("all", "全部范围", _topic_url(active, "all"), scope_counts.get("all", 0)),
                   ("brand", "品牌常青", _topic_url(active, "brand"), scope_counts.get("brand", 0))]
    scope_links.extend(
        (f"campaign:{c.id}", f"活动·{c.name}", _topic_url(active, f"campaign:{c.id}"),
         scope_counts.get(f"campaign:{c.id}", 0))
        for c in campaigns
    )
    return templates.TemplateResponse(request, "topic/home.html", {
        "brand": brand, "campaigns": campaigns, "topics": topics, "cmap": cmap,
        "statuses": TOPIC_STATUSES, "catalog": sources.catalog(),
        "tabs": TABS, "tab_counts": tab_counts, "tab_links": tab_links,
        "scope_links": scope_links, "active_tab": active, "active_scope": active_scope,
        "error": error, "modal_error": modal_error})


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
    try:
        generate_topics(session, brand.id, cid, count=min(max(count, 1), 10),
                        sources_used=valid, hot_query=hot_query,
                        use_rejection_experience=True)
    except ModelRateLimited:
        target_scope = "brand" if cid is None else f"campaign:{cid}"
        params = urlencode({"status": "候选", "scope": target_scope, "modal_error": "当前模型已限流"})
        return RedirectResponse(f"/topics?{params}", status_code=303)
    except ValueError as exc:
        target_scope = "brand" if cid is None else f"campaign:{cid}"
        params = urlencode({"status": "候选", "scope": target_scope, "error": f"生成失败：{exc}"})
        return RedirectResponse(f"/topics?{params}", status_code=303)
    target_scope = "brand" if cid is None else f"campaign:{cid}"
    return RedirectResponse(_topic_url("候选", target_scope), status_code=303)


@router.post("/topics/manual")
def manual_topics(request: Request, campaign_id: str = Form(""),
                  title: list[str] = Form([]),
                  session: Session = Depends(get_session)):
    """手动上传选题标题；标题原样保留，AI 只补全纲要/受众等字段。"""
    auth.require_level(request, 1)
    brand = _brand_or_404(session)
    cid = int(campaign_id) if campaign_id.strip() else None
    if cid is not None and session.get(Campaign, cid) is None:
        raise HTTPException(404, "活动不存在")
    target_scope = "brand" if cid is None else f"campaign:{cid}"
    try:
        create_manual_topics(session, brand.id, cid, title)
    except ValueError as exc:
        params = urlencode({"status": "候选", "scope": target_scope, "error": f"手动上传失败：{exc}"})
        return RedirectResponse(f"/topics?{params}", status_code=303)
    return RedirectResponse(_topic_url("候选", target_scope), status_code=303)


@router.post("/topics/{topic_id}/adopt")
def adopt(topic_id: int, request: Request, status: str = "all", scope: str = "all",
          session: Session = Depends(get_session)):
    """采纳候选：候选 → 采纳(待写作)。"""
    auth.require_level(request, 1)
    t = session.get(Topic, topic_id)
    if not t:
        raise HTTPException(404, "选题不存在")
    if t.status == "候选":
        t.status = "采纳"
        session.add(t)
        session.commit()
    return RedirectResponse(_topic_url(status, scope), status_code=303)


@router.post("/topics/{topic_id}/unadopt")
def unadopt(topic_id: int, request: Request, status: str = "all", scope: str = "all",
            session: Session = Depends(get_session)):
    """取消采纳：采纳 → 候选。仅②内部状态回退；③写作引擎已接手的选题不应回退（待③接入后加守卫）。"""
    auth.require_level(request, 1)
    t = session.get(Topic, topic_id)
    if not t:
        raise HTTPException(404, "选题不存在")
    if t.status == "采纳":
        t.status = "候选"
        session.add(t)
        session.commit()
    return RedirectResponse(_topic_url(status, scope), status_code=303)


@router.post("/topics/{topic_id}/delete")
def delete(topic_id: int, request: Request, status: str = "回收站", scope: str = "all",
           rejection_reason: str = Form(""),
           session: Session = Depends(get_session)):
    """删除进入回收站；必须记录不采纳原因，供同 campaign 后续生成选题时参考。"""
    auth.require_level(request, 1)
    t = session.get(Topic, topic_id)
    if t:
        reason = " ".join(rejection_reason.split())
        if not reason:
            raise HTTPException(400, "请填写不采纳原因")
        t.status = "回收站"
        t.rejection_reason = reason
        t.rejected_at = datetime.now()
        session.add(t)
        session.commit()
    return RedirectResponse(_topic_url("回收站", scope), status_code=303)
