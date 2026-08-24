"""①知识库 路由——样板模块：CRUD + 文件上传 + AI 解析 + 数据池，全走 core 抽象。

权限（ARCHITECTURE §7）：写操作 = 定义者(owner, level 2)；浏览/下载 = 所有登录角色。
写操作一律 `auth.require_level(request, 2)` 服务端守卫，模板再按 level 显隐。
"""
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.requests import Request
from sqlmodel import Session, select

from app import __version__
from app.core import auth, config, docparse, llm, runtime, storage
from app.core.db import get_session
from app.core.templates import create_templates
from app.modules.feedback.models import FeedbackExperience
from app.modules.knowledge import analysis
from app.modules.knowledge.experience_pool import (
    campaign_experience_pack_options, sync_all_campaign_experience_packs,
    sync_brand_experience_pack, sync_campaign_experience_pack,
)
from app.modules.knowledge.models import (
    Brand, BrandDoc, Campaign, CampaignDoc, CampaignPoolRef, CampaignStrategyRef,
    PoolTopic, Strategy, StrategyDoc,
)

router = APIRouter()
templates = create_templates()
_DEMO_HTML = Path("app/templates/demo.html")  # 只读演示壳（原型全貌，纯静态）

_ANALYZE_CHARS = 12000  # AI 解析喂给 LLM 的最大字符数（防超长）
_DEFAULT_BRAND_NAME = "溯肤"  # 单品牌默认（新增/删除品牌 UI 已隐藏）
_ACTIVITY_TYPES = {"campaign", "column"}


def _parse_date(value: str) -> date | None:
    value = (value or "").strip()
    return date.fromisoformat(value) if value else None


def _default_brand(session: Session) -> Brand:
    """取默认品牌；无则建「溯肤」。campaign 由用户自建（品牌日常已去除，品牌库已承载品牌内容）。"""
    brand = session.exec(select(Brand).order_by(Brand.id)).first()
    if brand is None:
        brand = Brand(name=_DEFAULT_BRAND_NAME)
        session.add(brand)
        session.commit()
        session.refresh(brand)
    return brand


# ─────────────────────────────── 品牌 ───────────────────────────────

@router.get("/")
def home(request: Request, session: Session = Depends(get_session)):
    # 只读演示：整个原型全貌当演示壳（六模块 tab + 各屏静态假数据，纯静态、不读 DB、不经 Jinja）。
    # 顶栏融了真实 app 的「模型配置」+「退出登录」。后端 CRUD 代码保留，维护接口仍可切回动态。
    if not runtime.knowledge_writable():
        html = _DEMO_HTML.read_text(encoding="utf-8").replace("__APP_VERSION__", __version__)
        if not auth.can_model_config(getattr(request.state, "role", None)):
            html = html.replace("· 右侧「模型配置」已可真用", "")
            html = html.replace(" · 右上角「模型配置」已可真用", "")
            html = html.replace("· 右上角「模型配置」已可真用", "")
            html = html.replace("· 模型配置已可真用", "")
            html = html.replace(
                '<a href="/settings/llm" class="text-brand-600 hover:text-brand-700 font-medium">模型配置</a>\n      ',
                "",
            )
        if not auth.can_view_module(getattr(request.state, "role", None), "permissions"):
            html = html.replace(",['perm','⑥权限']", "")
        return HTMLResponse(html)
    brand = _default_brand(session)          # 单品牌：默认「溯肤」
    campaigns = session.exec(
        select(Campaign).where(Campaign.brand_id == brand.id)
        .order_by(Campaign.is_default.desc(), Campaign.id)).all()
    experience_pack_options = campaign_experience_pack_options(session, brand.id)
    return templates.TemplateResponse(
        request, "knowledge/home.html", {
            "brand": brand,
            "campaigns": campaigns,
            "experience_pack_options": experience_pack_options,
        })


@router.post("/brands")
def create_brand(request: Request, name: str = Form(...),
                 session: Session = Depends(get_session)):
    auth.require_level(request, 2)
    brand = Brand(name=name)
    session.add(brand)
    session.commit()
    session.refresh(brand)
    return RedirectResponse(f"/brands/{brand.id}", status_code=303)


@router.get("/brands/{brand_id}")
def brand_detail(brand_id: int, request: Request, session: Session = Depends(get_session)):
    if not runtime.knowledge_writable():                     # 只读：二级页并入静态框架单页
        return RedirectResponse("/", status_code=303)
    brand = session.get(Brand, brand_id)
    if not brand:
        raise HTTPException(404, "品牌不存在")
    docs = session.exec(
        select(BrandDoc).where(BrandDoc.brand_id == brand_id)
        .order_by(BrandDoc.id.desc())).all()
    # 品牌定义页 = 主题调性/内容要求 + 文档 + AI 解析（campaign 挪到首页）
    return templates.TemplateResponse(request, "knowledge/brand.html",
                                      {"brand": brand, "docs": docs})


@router.post("/brands/{brand_id}/define")
def save_brand_define(brand_id: int, request: Request,
                      brand_prompt: str = Form(""), content_notes: str = Form(""),
                      session: Session = Depends(get_session)):
    """保存品牌定义（主题调性 Prompt + 内容要求）。"""
    auth.require_level(request, 2)
    brand = session.get(Brand, brand_id)
    if not brand:
        raise HTTPException(404, "品牌不存在")
    brand.brand_prompt = brand_prompt
    brand.content_notes = content_notes
    session.add(brand)
    session.commit()
    return RedirectResponse(f"/brands/{brand_id}", status_code=303)


@router.get("/brands/{brand_id}/docs/{doc_id}/download")
def download_brand_doc(brand_id: int, doc_id: int, session: Session = Depends(get_session)):
    doc = session.get(BrandDoc, doc_id)
    if not doc or doc.brand_id != brand_id or not Path(doc.file_path).exists():
        raise HTTPException(404, "文档不存在")
    return FileResponse(doc.file_path, filename=doc.filename)


@router.post("/brands/{brand_id}/docs/{doc_id}/delete")
def delete_brand_doc(brand_id: int, doc_id: int, request: Request,
                     session: Session = Depends(get_session)):
    auth.require_level(request, 2)
    doc = session.get(BrandDoc, doc_id)
    if not doc or doc.brand_id != brand_id:
        raise HTTPException(404, "文档不存在")
    Path(doc.file_path).unlink(missing_ok=True)
    session.delete(doc)
    session.commit()
    return RedirectResponse(f"/brands/{brand_id}", status_code=303)


@router.post("/brands/{brand_id}/docs")
def upload_brand_doc(brand_id: int, request: Request,
                     file: UploadFile = File(...),
                     session: Session = Depends(get_session)):
    auth.require_level(request, 2)
    brand = session.get(Brand, brand_id)
    if not brand:
        raise HTTPException(404, "品牌不存在")
    path = storage.save_upload(file, subdir=f"brand/{brand_id}")
    session.add(BrandDoc(brand_id=brand_id, filename=file.filename, file_path=path,
                         extracted_text=docparse.extract_text(path)))
    session.commit()
    return RedirectResponse(f"/brands/{brand_id}", status_code=303)


@router.post("/brands/{brand_id}/analyze")
def analyze_brand(brand_id: int, request: Request, session: Session = Depends(get_session)):
    """AI 解析资料文档（后台）：单篇解读 + 深度读图 → 文档解读综合/视觉风格 → 反推调性/要求。"""
    auth.require_level(request, 2)
    brand = session.get(Brand, brand_id)
    if not brand:
        raise HTTPException(404, "品牌不存在")
    if brand.analysis_status != "running":
        resume = brand.analysis_status == "failed"
        brand.analysis_status, brand.analysis_error = "running", ""
        session.add(brand)
        session.commit()
        analysis.start_background_analysis(brand_id, resume=resume)
    return RedirectResponse(f"/brands/{brand_id}", status_code=303)


@router.get("/brands/{brand_id}/analysis-status")
def brand_analysis_status(brand_id: int, request: Request,
                          session: Session = Depends(get_session)):
    """HTMX 轮询：running 返回自轮询片段；done/failed 触发整页刷新（看填充的字段/解读）。"""
    brand = session.get(Brand, brand_id)
    if not brand:
        raise HTTPException(404, "品牌不存在")
    if brand.analysis_status == "running":
        return templates.TemplateResponse(request, "knowledge/_analysis_poll.html",
                                          {"brand": brand})
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.post("/brands/{brand_id}/docs/{doc_id}/deep-read")
def toggle_deep_read(brand_id: int, doc_id: int, request: Request,
                     session: Session = Depends(get_session)):
    """切换某文档的「深度读图」（需读图的文档用 vision）。"""
    auth.require_level(request, 2)
    doc = session.get(BrandDoc, doc_id)
    if not doc or doc.brand_id != brand_id:
        raise HTTPException(404, "文档不存在")
    doc.deep_read = not doc.deep_read
    session.add(doc)
    session.commit()
    return RedirectResponse(f"/brands/{brand_id}", status_code=303)


# ─────────────────────────────── 策略 ───────────────────────────────

@router.get("/strategies")
def strategy_list(request: Request, session: Session = Depends(get_session)):
    if not runtime.knowledge_writable():
        return RedirectResponse("/", status_code=303)
    brand = _default_brand(session)
    strategies = session.exec(
        select(Strategy).where(Strategy.brand_id == brand.id)
        .order_by(Strategy.created_at.desc(), Strategy.id.desc())
    ).all()
    doc_counts = {
        strategy.id: len(session.exec(
            select(StrategyDoc.id).where(StrategyDoc.strategy_id == strategy.id)
        ).all())
        for strategy in strategies
    }
    return templates.TemplateResponse(request, "knowledge/strategies.html", {
        "brand": brand, "strategies": strategies, "doc_counts": doc_counts,
    })


@router.post("/strategies")
def create_strategy(request: Request, name: str = Form(...), description: str = Form(""),
                    session: Session = Depends(get_session)):
    auth.require_level(request, 2)
    brand = _default_brand(session)
    name = name.strip()
    if not name:
        raise HTTPException(422, "策略名称不能为空")
    strategy = Strategy(brand_id=brand.id, name=name, description=description.strip())
    session.add(strategy)
    session.commit()
    session.refresh(strategy)
    return RedirectResponse(f"/strategies/{strategy.id}", status_code=303)


@router.get("/strategies/{strategy_id}")
def strategy_detail(strategy_id: int, request: Request,
                    session: Session = Depends(get_session)):
    if not runtime.knowledge_writable():
        return RedirectResponse("/", status_code=303)
    strategy = session.get(Strategy, strategy_id)
    if strategy is None:
        raise HTTPException(404, "策略不存在")
    brand = session.get(Brand, strategy.brand_id)
    docs = session.exec(
        select(StrategyDoc).where(StrategyDoc.strategy_id == strategy_id)
        .order_by(StrategyDoc.id.desc())
    ).all()
    return templates.TemplateResponse(request, "knowledge/strategy.html", {
        "brand": brand, "strategy": strategy, "docs": docs,
    })


@router.post("/strategies/{strategy_id}/docs")
def upload_strategy_doc(strategy_id: int, request: Request, file: UploadFile = File(...),
                        session: Session = Depends(get_session)):
    auth.require_level(request, 2)
    strategy = session.get(Strategy, strategy_id)
    if strategy is None:
        raise HTTPException(404, "策略不存在")
    path = storage.save_upload(file, subdir=f"strategy/{strategy_id}")
    session.add(StrategyDoc(
        strategy_id=strategy_id,
        filename=file.filename,
        file_path=path,
        extracted_text=docparse.extract_text(path),
    ))
    strategy.analysis_status = "idle"
    session.add(strategy)
    session.commit()
    return RedirectResponse(f"/strategies/{strategy_id}", status_code=303)


@router.get("/strategies/{strategy_id}/docs/{doc_id}/download")
def download_strategy_doc(strategy_id: int, doc_id: int,
                          session: Session = Depends(get_session)):
    doc = session.get(StrategyDoc, doc_id)
    if doc is None or doc.strategy_id != strategy_id or not Path(doc.file_path).exists():
        raise HTTPException(404, "文档不存在")
    return FileResponse(doc.file_path, filename=doc.filename)


@router.post("/strategies/{strategy_id}/docs/{doc_id}/delete")
def delete_strategy_doc(strategy_id: int, doc_id: int, request: Request,
                        session: Session = Depends(get_session)):
    auth.require_level(request, 2)
    doc = session.get(StrategyDoc, doc_id)
    if doc is None or doc.strategy_id != strategy_id:
        raise HTTPException(404, "文档不存在")
    Path(doc.file_path).unlink(missing_ok=True)
    session.delete(doc)
    strategy = session.get(Strategy, strategy_id)
    if strategy:
        strategy.analysis_status = "idle"
        session.add(strategy)
    session.commit()
    return RedirectResponse(f"/strategies/{strategy_id}", status_code=303)


@router.post("/strategies/{strategy_id}/docs/{doc_id}/deep-read")
def toggle_strategy_deep_read(strategy_id: int, doc_id: int, request: Request,
                              session: Session = Depends(get_session)):
    auth.require_level(request, 2)
    doc = session.get(StrategyDoc, doc_id)
    if doc is None or doc.strategy_id != strategy_id:
        raise HTTPException(404, "文档不存在")
    doc.deep_read = not doc.deep_read
    session.add(doc)
    session.commit()
    return RedirectResponse(f"/strategies/{strategy_id}", status_code=303)


@router.post("/strategies/{strategy_id}/analyze")
def analyze_strategy(strategy_id: int, request: Request,
                     session: Session = Depends(get_session)):
    auth.require_level(request, 2)
    strategy = session.get(Strategy, strategy_id)
    if strategy is None:
        raise HTTPException(404, "策略不存在")
    if strategy.analysis_status != "running":
        strategy.analysis_status, strategy.analysis_error = "running", ""
        session.add(strategy)
        session.commit()
        analysis.start_strategy_analysis(strategy_id)
    return RedirectResponse(f"/strategies/{strategy_id}", status_code=303)


@router.get("/strategies/{strategy_id}/analysis-status")
def strategy_analysis_status(strategy_id: int, request: Request,
                             session: Session = Depends(get_session)):
    strategy = session.get(Strategy, strategy_id)
    if strategy is None:
        raise HTTPException(404, "策略不存在")
    if strategy.analysis_status == "running":
        return templates.TemplateResponse(request, "knowledge/_strategy_poll.html", {
            "strategy": strategy,
        })
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.post("/strategies/{strategy_id}/delete")
def delete_strategy(strategy_id: int, request: Request,
                    session: Session = Depends(get_session)):
    auth.require_level(request, 2)
    strategy = session.get(Strategy, strategy_id)
    if strategy is None:
        raise HTTPException(404, "策略不存在")
    for doc in session.exec(
        select(StrategyDoc).where(StrategyDoc.strategy_id == strategy_id)
    ).all():
        Path(doc.file_path).unlink(missing_ok=True)
        session.delete(doc)
    for ref in session.exec(
        select(CampaignStrategyRef).where(CampaignStrategyRef.strategy_id == strategy_id)
    ).all():
        session.delete(ref)
    session.delete(strategy)
    session.commit()
    return RedirectResponse("/strategies", status_code=303)


# ─────────────────────────────── Campaign ───────────────────────────────

@router.post("/campaigns")
def create_campaign(request: Request,
                    brand_id: int = Form(...), name: str = Form(...),
                    activity_type: str = Form("campaign"),
                    start_date: str = Form(""), end_date: str = Form(""),
                    experience_pack_id: list[int] = Form([]),
                    session: Session = Depends(get_session)):
    auth.require_level(request, 2)
    if not session.get(Brand, brand_id):
        raise HTTPException(404, "品牌不存在")
    if activity_type not in _ACTIVITY_TYPES:
        raise HTTPException(422, "活动类型无效")
    campaign = Campaign(brand_id=brand_id, name=name.strip(),
                        activity_type=activity_type,
                        start_date=None if activity_type == "column" else _parse_date(start_date),
                        end_date=_parse_date(end_date))
    session.add(campaign)
    session.commit()
    session.refresh(campaign)
    for pool_topic_id in dict.fromkeys(experience_pack_id):
        topic = session.get(PoolTopic, pool_topic_id)
        if topic is None or topic.kind != "经验包":
            continue
        session.add(CampaignPoolRef(campaign_id=campaign.id, pool_topic_id=pool_topic_id))
    if experience_pack_id:
        session.commit()
    return RedirectResponse(f"/campaigns/{campaign.id}", status_code=303)


@router.get("/campaigns/{campaign_id}")
def campaign_detail(campaign_id: int, request: Request,
                    session: Session = Depends(get_session)):
    if not runtime.knowledge_writable():                     # 只读：并入静态框架单页
        return RedirectResponse("/", status_code=303)
    campaign = session.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404, "活动不存在")
    brand = session.get(Brand, campaign.brand_id)
    docs = session.exec(
        select(CampaignDoc).where(CampaignDoc.campaign_id == campaign_id)
        .order_by(CampaignDoc.id.desc())).all()
    # 已引用的数据池条目 + 可引用的（未引用的池子条目，供勾选）
    ref_ids = [r.pool_topic_id for r in session.exec(
        select(CampaignPoolRef).where(CampaignPoolRef.campaign_id == campaign_id)).all()]
    all_topics = session.exec(select(PoolTopic).order_by(PoolTopic.id.desc())).all()
    refs = [t for t in all_topics if t.id in ref_ids]
    available = [t for t in all_topics if t.id not in ref_ids]
    strategy_ref_ids = {
        ref.strategy_id for ref in session.exec(
            select(CampaignStrategyRef).where(CampaignStrategyRef.campaign_id == campaign_id)
        ).all()
    }
    strategies = session.exec(
        select(Strategy).where(Strategy.brand_id == campaign.brand_id)
        .order_by(Strategy.created_at.desc(), Strategy.id.desc())
    ).all()
    return templates.TemplateResponse(request, "knowledge/campaign.html",
                                      {"campaign": campaign, "brand": brand, "docs": docs,
                                       "refs": refs, "available": available,
                                       "strategies": strategies,
                                       "strategy_ref_ids": strategy_ref_ids})


@router.post("/campaigns/{campaign_id}/context")
def save_campaign_context(campaign_id: int, request: Request,
                          strategy_id: list[int] = Form([]),
                          brand_weight: int = Form(3),
                          strategy_weight: int = Form(3),
                          activity_weight: int = Form(4),
                          session: Session = Depends(get_session)):
    auth.require_level(request, 2)
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(404, "活动不存在")
    weights = (brand_weight, strategy_weight, activity_weight)
    if any(weight < 0 or weight > 100 for weight in weights) or sum(weights) == 0:
        raise HTTPException(422, "权重需为 0-100 的整数，且不能全部为 0")
    selected_ids = set(strategy_id)
    selected = session.exec(
        select(Strategy).where(Strategy.id.in_(selected_ids))
    ).all() if selected_ids else []
    if len(selected) != len(selected_ids) or any(
        strategy.brand_id != campaign.brand_id for strategy in selected
    ):
        raise HTTPException(422, "只能引用当前品牌的策略")

    for ref in session.exec(
        select(CampaignStrategyRef).where(CampaignStrategyRef.campaign_id == campaign_id)
    ).all():
        session.delete(ref)
    for strategy in selected:
        session.add(CampaignStrategyRef(
            campaign_id=campaign_id, strategy_id=strategy.id,
        ))
    campaign.brand_weight = brand_weight
    campaign.strategy_weight = strategy_weight
    campaign.activity_weight = activity_weight
    session.add(campaign)
    session.commit()
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@router.get("/campaigns/{campaign_id}/docs/{doc_id}/download")
def download_campaign_doc(campaign_id: int, doc_id: int, session: Session = Depends(get_session)):
    doc = session.get(CampaignDoc, doc_id)
    if not doc or doc.campaign_id != campaign_id or not Path(doc.file_path).exists():
        raise HTTPException(404, "文档不存在")
    return FileResponse(doc.file_path, filename=doc.filename)


@router.post("/campaigns/{campaign_id}/docs/{doc_id}/delete")
def delete_campaign_doc(campaign_id: int, doc_id: int, request: Request,
                        session: Session = Depends(get_session)):
    auth.require_level(request, 2)
    doc = session.get(CampaignDoc, doc_id)
    if not doc or doc.campaign_id != campaign_id:
        raise HTTPException(404, "文档不存在")
    Path(doc.file_path).unlink(missing_ok=True)
    session.delete(doc)
    session.commit()
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@router.post("/campaigns/{campaign_id}/pool-refs")
def add_pool_ref(campaign_id: int, request: Request,
                 pool_topic_id: int = Form(...),
                 session: Session = Depends(get_session)):
    """引用一个数据池条目到本 campaign（不单独上传，只引用）。"""
    auth.require_level(request, 2)
    if not session.get(Campaign, campaign_id):
        raise HTTPException(404, "活动不存在")
    if not session.get(PoolTopic, pool_topic_id):
        raise HTTPException(404, "数据池条目不存在")
    exists = session.exec(
        select(CampaignPoolRef).where(CampaignPoolRef.campaign_id == campaign_id,
                                      CampaignPoolRef.pool_topic_id == pool_topic_id)).first()
    if not exists:
        session.add(CampaignPoolRef(campaign_id=campaign_id, pool_topic_id=pool_topic_id))
        session.commit()
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@router.post("/campaigns/{campaign_id}/pool-refs/{topic_id}/delete")
def remove_pool_ref(campaign_id: int, topic_id: int, request: Request,
                    session: Session = Depends(get_session)):
    auth.require_level(request, 2)
    ref = session.exec(
        select(CampaignPoolRef).where(CampaignPoolRef.campaign_id == campaign_id,
                                      CampaignPoolRef.pool_topic_id == topic_id)).first()
    if ref:
        session.delete(ref)
        session.commit()
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


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
                            file_path=path, note=note,
                            extracted_text=docparse.extract_text(path)))
    session.commit()
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@router.post("/campaigns/{campaign_id}/parse")
def parse_campaign(campaign_id: int, request: Request,
                   session: Session = Depends(get_session)):
    """AI 解析活动资料（后台，同 brand）：品牌定义 + 资料[含深度读图] + 引用数据池 → campaign_digest。"""
    auth.require_level(request, 2)
    campaign = session.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404, "活动不存在")
    if campaign.analysis_status != "running":
        campaign.analysis_status, campaign.analysis_error = "running", ""
        session.add(campaign)
        session.commit()
        analysis.start_campaign_analysis(campaign_id)
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@router.get("/campaigns/{campaign_id}/analysis-status")
def campaign_analysis_status(campaign_id: int, request: Request,
                             session: Session = Depends(get_session)):
    """HTMX 轮询：running 返回自轮询片段；done/failed 触发整页刷新看结果（同 brand）。"""
    campaign = session.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404, "活动不存在")
    if campaign.analysis_status == "running":
        return templates.TemplateResponse(request, "knowledge/_campaign_poll.html",
                                          {"campaign": campaign})
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.post("/campaigns/{campaign_id}/docs/{doc_id}/deep-read")
def toggle_campaign_deep_read(campaign_id: int, doc_id: int, request: Request,
                              session: Session = Depends(get_session)):
    """切换某活动资料的「深度读图」（同品牌资料，需读图的文档用 vision 读 PDF）。"""
    auth.require_level(request, 2)
    doc = session.get(CampaignDoc, doc_id)
    if not doc or doc.campaign_id != campaign_id:
        raise HTTPException(404, "文档不存在")
    doc.deep_read = not doc.deep_read
    session.add(doc)
    session.commit()
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@router.post("/campaigns/{campaign_id}/delete")
def delete_campaign(campaign_id: int, request: Request,
                    session: Session = Depends(get_session)):
    auth.require_level(request, 2)
    campaign = session.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404, "活动不存在")
    brand_id = campaign.brand_id
    for doc in session.exec(
            select(CampaignDoc).where(CampaignDoc.campaign_id == campaign_id)).all():
        session.delete(doc)
    for ref in session.exec(
            select(CampaignStrategyRef).where(CampaignStrategyRef.campaign_id == campaign_id)).all():
        session.delete(ref)
    session.delete(campaign)
    session.commit()
    # HTMX 调用走 HX-Redirect（客户端跳转，不 swap body）；普通请求走 303。
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": f"/brands/{brand_id}"})
    return RedirectResponse(f"/brands/{brand_id}", status_code=303)


# ─────────────────────────────── 数据池 ───────────────────────────────

@router.get("/pool")
def pool_list(request: Request, session: Session = Depends(get_session)):
    if not runtime.knowledge_writable():                     # 只读：并入静态框架单页
        return RedirectResponse("/", status_code=303)
    brand = _default_brand(session)
    campaign_packs = sync_all_campaign_experience_packs(session, brand.id)
    material_topics = session.exec(
        select(PoolTopic).where(PoolTopic.kind == "资料包").order_by(PoolTopic.id.desc())
    ).all()
    return templates.TemplateResponse(request, "knowledge/pool.html", {
        "material_topics": material_topics,
        "campaign_packs": campaign_packs,
    })


@router.post("/pool")
def create_pool_topic(request: Request,
                      title: str = Form(...), kind: str = Form("资料包"),
                      web_access: bool = Form(False), brand_tag: str = Form(""),
                      content: str = Form(""), file: UploadFile | None = File(None),
                      session: Session = Depends(get_session)):
    """新增数据池条目。可上传资料文件：抽取正文入 content（有手填正文则以手填为准），存原文件供下载。"""
    auth.require_level(request, 2)
    file_path = ""
    if file is not None and file.filename:
        file_path = storage.save_upload(file, subdir="pool")
        content = content or docparse.extract_text(file_path)   # 有手填正文用手填，否则用抽取文字
    topic = PoolTopic(title=title, kind=kind, web_access=web_access, source="upload",
                      brand_tag=brand_tag or None, content=content, file_path=file_path)
    session.add(topic)
    session.commit()
    return RedirectResponse("/pool", status_code=303)


@router.get("/pool/{topic_id}/download")
def download_pool_file(topic_id: int, session: Session = Depends(get_session)):
    topic = session.get(PoolTopic, topic_id)
    if not topic or not topic.file_path or not Path(topic.file_path).exists():
        raise HTTPException(404, "文件不存在")
    return FileResponse(topic.file_path, filename=Path(topic.file_path).name)


def _delete_pool_file_if_safe(file_path: str) -> None:
    if not file_path:
        return
    try:
        path = Path(file_path).resolve()
        data_root = Path(config.DATA_DIR).resolve()
        if path.exists() and (path == data_root or data_root in path.parents):
            path.unlink()
    except OSError:
        pass


@router.post("/pool/experience-articles/delete")
def delete_experience_article(request: Request,
                              source_slot_id: int | None = Form(None),
                              article_id: int | None = Form(None),
                              session: Session = Depends(get_session)):
    auth.require_level(request, 2)
    brand = _default_brand(session)
    conditions = [FeedbackExperience.brand_id == brand.id, FeedbackExperience.is_active == True]
    if source_slot_id:
        conditions.append(FeedbackExperience.source_slot_id == source_slot_id)
    elif article_id:
        conditions.append(FeedbackExperience.article_id == article_id)
    else:
        raise HTTPException(400, "缺少文章经验来源")
    entries = session.exec(select(FeedbackExperience).where(*conditions)).all()
    campaign_ids = {entry.campaign_id for entry in entries}
    for entry in entries:
        entry.is_active = False
        session.add(entry)
    session.commit()
    for campaign_id in campaign_ids:
        if campaign_id is None:
            sync_brand_experience_pack(session, brand.id)
        else:
            sync_campaign_experience_pack(session, campaign_id)
    return RedirectResponse("/pool", status_code=303)


@router.post("/pool/{topic_id}/delete")
def delete_pool_topic(topic_id: int, request: Request, session: Session = Depends(get_session)):
    auth.require_level(request, 2)
    topic = session.get(PoolTopic, topic_id)
    if not topic:
        return RedirectResponse("/pool", status_code=303)
    if topic.kind == "经验包" and topic.source == "feedback":
        raise HTTPException(400, "经验包不能整体删除，请删除单篇文章经验")

    refs = session.exec(select(CampaignPoolRef).where(CampaignPoolRef.pool_topic_id == topic_id)).all()
    for ref in refs:
        session.delete(ref)

    _delete_pool_file_if_safe(topic.file_path)
    session.delete(topic)
    session.commit()
    return RedirectResponse("/pool", status_code=303)


@router.post("/pool/{topic_id}/deep-read")
def toggle_pool_deep_read(topic_id: int, request: Request,
                          session: Session = Depends(get_session)):
    """切换数据池条目的「深度读图」（只对 PDF 有意义；图片自动读图、文字走正文）。"""
    auth.require_level(request, 2)
    topic = session.get(PoolTopic, topic_id)
    if not topic:
        raise HTTPException(404, "条目不存在")
    topic.deep_read = not topic.deep_read
    session.add(topic)
    session.commit()
    return RedirectResponse("/pool", status_code=303)


@router.get("/styleguide")
def styleguide(request: Request):
    """组件样例页——开发参考，展示设计系统所有组件。"""
    return templates.TemplateResponse(request, "styleguide.html", {})
