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
_IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "gif"}   # 图片：天生只能 vision，永远读图


def _ext(path: str) -> str:
    from pathlib import Path
    return Path(path).suffix.lower().lstrip(".") if path else ""


def _as_attachment(file_path: str, deep_read: bool) -> bool:
    """该文件是否作 vision 附件读：图片永远是；PDF 看 deep_read 开关；其余（docx/pptx/txt…）走文字。
    vision 只吃 PDF+图片——PPT/Word 要读图需先导出 PDF。"""
    e = _ext(file_path)
    if e in _IMAGE_EXTS:
        return True
    return e == "pdf" and bool(deep_read)


def _campaign_digest_prompt(material: str) -> str:
    return (
        "你是资深内容策划。品牌的调性/文风/写作规范/视觉风格已由下方「品牌定义」固定，本文不要重复。\n"
        "只针对这个**活动**，提炼一份有时效性的「活动选题简报」，供选题库产出高时效活动选题。\n"
        "⚠️ 活动类型不定（展览/节日促销/新品/快闪/联名…）——先判断是什么活动，再按它实际有的内容填；"
        "有则填、无则略、绝不编造。\n\n"
        "① 活动速览：一切精确可引用的关键事实（名称/时间/地点/参与方/规模/主张/产品/价格/优惠…有什么写什么）\n"
        "② 时效节点：活动的时间结构（预热/进行中/收尾各适合什么选题 + 可结合的时令/节日/热点）\n"
        "③ 可用选题方向：本活动具体角度，每条标注【受众·内容类型·时效强弱】\n"
        "④ 关键素材清单：可直接用于内容的具体素材（产品/展品/物件/人物/数据/卖点，带规格/价格/来源等，有则带）\n"
        "⑤ 配图素材：本活动可用图片（附件里的图/PDF）及适用场景\n"
        "⑥ 参考与经验（来自引用的数据池，属弱相关参考、不喧宾夺主）：\n"
        "   -「资料包」类：作为补充背景/佐证素材，指出能支撑上面哪些选题方向\n"
        "   -「经验包」类（过往复盘沉淀）：据此给打法建议——本次选题优先做什么（已验证有效）、规避什么（曾失效）\n\n"
        "用简体中文，结构清晰、选题库拿了直接能用。附件里的 PDF/图片请仔细读。\n\n" + material)


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

    # ── Phase 1：拼文本素材 + 收集 vision 附件，一次性调用（codex/claude 读 PDF+图片）──
    # 深度读图的活动资料、以及引用的「图片/PDF 型数据池」→ 作附件交给 vision；其余走抽取文字/content。
    parts = [f"活动：{campaign.name}"]
    if brand and brand.brand_prompt:
        parts.append(f"【品牌定义】\n{brand.brand_prompt}")
    attachments: list[str] = []
    for d in docs:
        if _as_attachment(d.file_path, d.deep_read):     # 图片自动 / PDF 勾深度读图 → vision
            attachments.append(d.file_path)
            parts.append(f"【资料·见附件｜{d.filename}｜{d.note}】")
        else:
            parts.append(f"【资料｜{d.filename}｜{d.note}】\n{d.extracted_text}")
    for t in refs:                                       # 数据池：带 kind(资料包/经验包)+触网，让 AI 分清用法
        tag = f"{t.kind}·{'触网' if t.web_access else '不触网'}"
        if _as_attachment(t.file_path, t.deep_read):     # 图片自动 / PDF 勾深度读图 → vision
            attachments.append(t.file_path)
            parts.append(f"【引用数据池·{tag}·见附件｜{t.title}】")
        else:
            parts.append(f"【引用数据池·{tag}｜{t.title}】\n{t.content}")
    material = "\n\n".join(parts)[:_ANALYZE_CHARS]
    digest = llm.generate_text(_campaign_digest_prompt(material),
                               task="campaign_digest", attachments=attachments)

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
