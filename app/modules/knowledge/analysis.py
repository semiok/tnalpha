"""品牌资料 AI 解析——照抄 tngen（app/documents/analysis.py），保持逻辑一致。

流程：每篇文档解读(ai_analysis) + 深度读图视觉(style_summary)
→ 聚合 doc_digest / style_digest → 反推 brand_prompt / content_notes（自动填，可被定义者改）。
慢（多次 LLM 调用），故走后台线程 + analysis_status 轮询。
"""
import re
import threading

from sqlmodel import Session, select

from app.core import db, llm
from app.core.llm import prompts
from app.modules.knowledge.models import (
    Brand, BrandDoc, Campaign, CampaignDoc, CampaignPoolRef, PoolTopic,
)

_ANALYZE_CHARS = 12000

# 活动资料深度读图提示（读 PDF 图片页；只 claude-cli/codex 真读图，其余 provider 忽略 pdf 按文本）
_CAMPAIGN_DOC_READ = (
    "请阅读这份活动资料（PDF，含图片页），提取对内容创作有用的信息："
    "活动主题、关键信息点、时间/地点节点、视觉风格与画面元素。用简体中文，分点。")


def _campaign_digest_prompt(material: str) -> str:
    return (
        "你是资深内容策划。基于以下某活动的品牌定义、活动资料、引用数据池，提炼一份"
        "「活动内容提示词」，供选题库据此推荐选题：包含①活动核心主题与卖点、②可用内容方向、"
        "③调性与表达要点、④关键素材/信息点。用简体中文，结构清晰、可直接引用。\n\n" + material)


def run_analysis(brand_id: int, session: Session) -> None:
    """重生成该品牌所有文档的解读 + 综合解读 + 反推品牌字段。单篇失败不中断整体。

    两段式：先做完全部 LLM 调用（收集内存），再一次性写库——避免 LLM 调用期间持有
    待写事务（llm.generate_text 每次开嵌套 DB session 读设置，会与外层事务冲突）。
    """
    brand = session.get(Brand, brand_id)
    if brand is None:
        raise ValueError("品牌不存在")
    docs = session.exec(
        select(BrandDoc).where(BrandDoc.brand_id == brand_id).order_by(BrandDoc.created_at)).all()

    # ── Phase 1：只做 LLM 调用，结果存内存 ──
    results = []  # (doc, ai_analysis, style_summary)
    for d in docs:
        content = (d.extracted_text or "")[:_ANALYZE_CHARS]
        ai = llm.generate_text(prompts.content_analysis(d.filename, content), task="doc_analysis")
        style = ""
        if d.deep_read:
            try:
                style = llm.generate_text(prompts.style_analysis(d.filename),
                                          task="style", pdf_path=d.file_path)
            except Exception as e:                       # 深度读图失败不拖垮整体
                style = f"[风格解析失败: {str(e)[:80]}]"
        results.append((d, ai, style))

    content_items = [(d.filename, ai) for d, ai, _ in results if ai]
    doc_digest = (llm.generate_text(prompts.aggregate_content(content_items), task="doc_digest")
                  if content_items else "")
    style_items = [(d.filename, st) for d, _, st in results
                   if st and not st.startswith("[风格解析失败")]
    style_digest = (llm.generate_text(prompts.aggregate_style(style_items), task="style_digest")
                    if style_items else "")
    # 基于 doc_digest 反推主题调性/内容要求（自动填，可被定义者改；失败则保持原值）
    brand_prompt, content_notes = brand.brand_prompt, brand.content_notes
    if doc_digest:
        try:
            bf = _parse_brand_fields(llm.generate_text(
                prompts.brand_fields_prompt(brand.name, doc_digest), task="brand_fields"))
            brand_prompt, content_notes = bf["brand_prompt"], bf["content_notes"]
        except Exception:
            pass

    # ── Phase 2：一次性写库（此时无 LLM 调用、无嵌套 session）──
    for d, ai, style in results:
        d.ai_analysis, d.style_summary = ai, style
        session.add(d)
    brand.doc_digest, brand.style_digest = doc_digest, style_digest
    brand.brand_prompt, brand.content_notes = brand_prompt, content_notes
    session.add(brand)
    session.commit()


def _parse_brand_fields(text: str) -> dict:
    t = (text or "").strip()
    mt = re.search(r"调性[:：]\s*(.*?)(?=\n\s*要求[:：]|$)", t, flags=re.S)
    mr = re.search(r"要求[:：]\s*(.+)", t, flags=re.S)
    brand = mt.group(1).strip() if mt else ""
    notes = mr.group(1).strip() if mr else ""
    if not brand or not notes:
        raise ValueError("解析品牌字段失败：缺调性或要求")
    return {"brand_prompt": brand, "content_notes": notes}


def start_background_analysis(brand_id: int) -> None:
    """置 running + 后台线程跑 run_analysis（独立 Session）；成功 done、异常 failed。"""
    def _worker() -> None:
        with Session(db.engine) as s:
            try:
                run_analysis(brand_id, s)
                brand = s.get(Brand, brand_id)
                brand.analysis_status, brand.analysis_error = "done", ""
                s.add(brand)
                s.commit()
            except Exception as e:
                s.rollback()
                brand = s.get(Brand, brand_id)
                if brand:
                    brand.analysis_status, brand.analysis_error = "failed", str(e)[:200]
                    s.add(brand)
                    s.commit()

    threading.Thread(target=_worker, daemon=True).start()


# ─────────────────────────── 活动（campaign）资料解析 ───────────────────────────

def run_campaign_analysis(campaign_id: int, session: Session) -> None:
    """AI 解析活动资料 → campaign_digest（供②选题库读）。两段式（LLM 调用 → 一次性写库）。

    深度读图（deep_read）的文档：读 PDF 图片页（同品牌资料）；其余用抽取正文。
    """
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise ValueError("活动不存在")
    docs = session.exec(
        select(CampaignDoc).where(CampaignDoc.campaign_id == campaign_id)
        .order_by(CampaignDoc.id)).all()
    brand = session.get(Brand, campaign.brand_id)
    ref_ids = [r.pool_topic_id for r in session.exec(
        select(CampaignPoolRef).where(CampaignPoolRef.campaign_id == campaign_id)).all()]
    refs = session.exec(select(PoolTopic).where(PoolTopic.id.in_(ref_ids))).all() if ref_ids else []

    # ── Phase 1：LLM 调用（深度读图文档读 PDF）──
    parts = [f"活动：{campaign.name}"]
    if brand and brand.brand_prompt:
        parts.append(f"【品牌定义】\n{brand.brand_prompt}")
    for d in docs:
        if d.deep_read:
            try:
                desc = llm.generate_text(f"{_CAMPAIGN_DOC_READ}\n\n文件名：{d.filename}",
                                         task="campaign_doc", pdf_path=d.file_path)
            except Exception as e:                       # 读图失败不拖垮整体，退回抽取文字
                desc = f"[读图失败: {str(e)[:80]}]\n{d.extracted_text}"
            parts.append(f"【资料·读图｜{d.filename}｜{d.note}】\n{desc}")
        else:
            parts.append(f"【资料｜{d.filename}｜{d.note}】\n{d.extracted_text}")
    parts += [f"【引用数据池｜{t.title}】\n{t.content}" for t in refs]
    material = "\n\n".join(parts)[:_ANALYZE_CHARS]
    digest = llm.generate_text(_campaign_digest_prompt(material), task="campaign_digest")

    # ── Phase 2：一次性写库 ──
    campaign.campaign_digest = digest
    session.add(campaign)
    session.commit()


def start_campaign_analysis(campaign_id: int) -> None:
    """置 running + 后台线程跑 run_campaign_analysis；成功 done、异常 failed。"""
    def _worker() -> None:
        with Session(db.engine) as s:
            try:
                run_campaign_analysis(campaign_id, s)
                c = s.get(Campaign, campaign_id)
                c.analysis_status, c.analysis_error = "done", ""
                s.add(c)
                s.commit()
            except Exception as e:
                s.rollback()
                c = s.get(Campaign, campaign_id)
                if c:
                    c.analysis_status, c.analysis_error = "failed", str(e)[:200]
                    s.add(c)
                    s.commit()

    threading.Thread(target=_worker, daemon=True).start()
