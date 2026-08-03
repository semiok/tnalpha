"""④排期版：审核通过文章 → 发布周排期 → 发布回填。"""
import io
import os
import re
import urllib.request
import zipfile
from datetime import date, datetime
from urllib.parse import quote, unquote, urlencode

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse, Response
from sqlmodel import Session, select
from starlette.requests import Request

from app.core import auth, config
from app.core.db import get_session
from app.core.templates import create_templates
from app.modules.knowledge.models import Campaign
from app.modules.schedule import schedule
from app.modules.schedule.models import ScheduleSlot, ScheduleWeek
from app.modules.topic.models import Topic
from app.modules.writing.models import Article, ArticleImage

router = APIRouter()
templates = create_templates()

LIBRARY_TABS = [
    ("all", "全部", None),
    ("可排期", "可排期", "可排期"),
    ("已排期", "已排期", "已排期"),
    ("已发布", "已发布", "已发布"),
]


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


def _public_image_url(url_or_path: str) -> str:
    value = (url_or_path or "").strip()
    if not value:
        return ""
    if value.startswith(("http://", "https://", "data:", "/writing/uploads/", "/static/")):
        return value
    real = os.path.realpath(value)
    data_root = os.path.realpath(config.DATA_DIR)
    if real == data_root or real.startswith(data_root + os.sep):
        rel = os.path.relpath(real, data_root)
        return f"/writing/uploads/{quote(rel, safe='/')}"
    return value


def _article_images(session: Session, article: Article) -> list[dict[str, str | int]]:
    rows = session.exec(
        select(ArticleImage)
        .where(ArticleImage.article_id == article.id, ArticleImage.is_selected == True)
        .order_by(ArticleImage.slot_index, ArticleImage.id)
    ).all()
    seen: set[str] = set()
    images: list[dict[str, str]] = []
    for row in rows:
        url = _public_image_url(row.image_url)
        if not url or url in seen:
            continue
        seen.add(url)
        images.append({
            "url": url,
            "label": row.slot_desc or row.prompt or f"配图 {row.slot_index + 1}",
            "slot_index": row.slot_index,
        })
    fallback = _public_image_url(article.image_url)
    if fallback and fallback not in seen:
        images.insert(0, {"url": fallback, "label": article.image_prompt or "主图", "slot_index": 0})
    return images


def _publish_body(body: str) -> str:
    text = re.sub(r"\n?\[插图(?:位|位置)?[：:].+?\]\n?", "\n\n", body or "")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _body_segments_with_images(body: str, images: list[dict[str, str | int]]) -> list[dict]:
    slots: dict[int, list[dict[str, str | int]]] = {}
    for image in images:
        try:
            slot_index = int(image.get("slot_index", 0))
        except (TypeError, ValueError):
            slot_index = 0
        slots.setdefault(slot_index, []).append(image)

    result: list[dict] = []
    pattern = re.compile(r"\[插图(?:位|位置)?[：:](.+?)\]")
    last_end = 0
    slot_index = 0
    for match in pattern.finditer(body or ""):
        text = (body or "")[last_end:match.start()].strip()
        if text:
            result.append({"type": "text", "text": text})
        result.append({
            "type": "slot",
            "slot_index": slot_index,
            "desc": match.group(1).strip(),
            "images": slots.get(slot_index, []),
        })
        slot_index += 1
        last_end = match.end()
    tail = (body or "")[last_end:].strip()
    if tail:
        result.append({"type": "text", "text": tail})

    if slot_index == 0 and images:
        for idx in sorted(slots):
            result.append({
                "type": "slot",
                "slot_index": idx,
                "desc": str((slots[idx][0].get("label") if slots[idx] else "") or "文章配图"),
                "images": slots[idx],
            })
    return result


# ── AI 排版：分平台输出 ──

def _plain_body(body: str) -> str:
    """去掉插图标记 + 开头的「标题：」「正文：」占位行，返回纯正文。"""
    text = re.sub(r"\n?\[插图(?:位|位置)?[：:].+?\]\n?", "\n\n", body or "")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    lines = text.split("\n")
    cleaned: list[str] = []
    for ln in lines:
        s = ln.strip()
        if s in ("标题：", "正文：", "标题:", "正文:"):
            continue
        if s.startswith("标题：") or s.startswith("标题:"):
            continue
        cleaned.append(ln)
    return "\n".join(cleaned).strip()


# 小红书关键句加 emoji 的锚点词（命中即在该句首/尾加一个轻 emoji）
_XHS_EMOJI_MAP = [
    ("秘密", "🔍"), ("革命", "🔥"), ("极端", "⚡"), ("历史", "📜"),
    ("第一次", "✨"), ("从未", "💫"), ("真正", "🌟"), ("发现", "💡"),
    ("背后", "👀"), ("答案", "🎯"), ("为什么", "❓"), ("如何", "🤔"),
    ("然而", "↩️"), ("但是", "⚡"), ("本质", "💎"), ("核心", "🔑"),
    ("身体", "🌿"), ("舒适", "☁️"), ("东方", "🍵"), ("皮肤", "🌸"),
    ("材料", "🧵"), ("设计", "✏️"), ("睡眠", "🌙"), ("温度", "🌡️"),
]


def _format_xhs(title: str, body: str, images: list[dict]) -> dict:
    """小红书版：剥掉插图标记 → 长段切短句 → 关键句加 emoji → 文末补话题标签 → 超字数红字提示。"""
    plain = _plain_body(body)
    # 按段落切，每段再按句号/问号/感叹号切成短行
    paragraphs = [p.strip() for p in plain.split("\n\n") if p.strip()]
    lines: list[str] = []
    for para in paragraphs:
        sentences = re.split(r"(?<=[。！？])", para)
        for s in sentences:
            s = s.strip()
            if not s:
                continue
            # 关键句加 emoji
            for kw, emoji in _XHS_EMOJI_MAP:
                if kw in s:
                    s = f"{emoji} {s}"
                    break
            lines.append(s)
        lines.append("")  # 段间空行
    # 标签：从标题提取关键词
    tags: list[str] = []
    title_clean = re.sub(r"[，。？！,.?!:：""\"'（）()【】\[\]]+", " ", title)
    for w in title_clean.split():
        if len(w) >= 2 and w not in tags:
            tags.append(w)
    # 补几个通用标签
    tags.extend(["#深度阅读", "#知识分享"])
    tag_line = " ".join(f"#{t}#" for t in tags[:6])
    content = "\n".join(lines).strip() + "\n\n" + tag_line
    char_count = len(re.sub(r"\s", "", content))
    over_limit = char_count > 1000
    image_hint = ""
    if images:
        image_hint = f"📎 本文共 {len(images)} 张配图，请单独上传到小红书图集"
    return {
        "content": content,
        "char_count": char_count,
        "over_limit": over_limit,
        "image_hint": image_hint,
    }


def _html_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _format_wechat(title: str, body: str, images: list[dict]) -> str:
    """公众号版：纯文本转带样式 HTML，插图标记替换成 <img>。"""
    # 按插图标记拆分
    pattern = re.compile(r"\[插图(?:位|位置)?[：:](.+?)\]")
    # 构建 slot_index → image url 的映射
    slot_urls: list[str] = []
    for i, match in enumerate(pattern.finditer(body or "")):
        if i < len(images):
            slot_urls.append(images[i].get("url", ""))
        else:
            slot_urls.append("")

    html_parts: list[str] = []
    html_parts.append(
        f'<section style="font-size:15px;line-height:1.8;color:#3f3f3f;letter-spacing:0.5px;">'
    )
    # 标题
    html_parts.append(
        f'<h1 style="font-size:20px;font-weight:bold;color:#1a1a1a;text-align:center;'
        f'margin:0 0 20px 0;line-height:1.4;">{_html_escape(title)}</h1>'
    )

    last_end = 0
    slot_idx = 0
    for match in pattern.finditer(body or ""):
        text = (body or "")[last_end:match.start()].strip()
        if text:
            # 去掉「标题：」「正文：」占位行
            text = re.sub(r"\n?标题[：:][^\n]*\n?", "\n", text)
            text = re.sub(r"\n?正文[：:]\s*\n?", "\n", text)
            for block in text.split("\n\n"):
                block = block.strip()
                if not block:
                    continue
                # 小标题：短行且无句号
                if len(block) <= 20 and not re.search(r"[。！？]", block):
                    html_parts.append(
                        f'<h2 style="font-size:17px;font-weight:bold;color:#1a1a1a;'
                        f'border-left:3px solid #5a7a8a;padding-left:10px;margin:24px 0 12px 0;">'
                        f'{_html_escape(block)}</h2>'
                    )
                else:
                    html_parts.append(
                        f'<p style="margin:0 0 16px 0;text-indent:2em;">{_html_escape(block)}</p>'
                    )
        # 插图
        if slot_idx < len(slot_urls) and slot_urls[slot_idx]:
            html_parts.append(
                f'<figure style="margin:20px 0;text-align:center;">'
                f'<img src="{slot_urls[slot_idx]}" style="max-width:100%;border-radius:4px;" />'
                f'<figcaption style="font-size:13px;color:#999;margin-top:6px;">'
                f'{_html_escape(match.group(1).strip())}</figcaption></figure>'
            )
        slot_idx += 1
        last_end = match.end()

    tail = (body or "")[last_end:].strip()
    if tail:
        tail = re.sub(r"\n?标题[：:][^\n]*\n?", "\n", tail)
        tail = re.sub(r"\n?正文[：:]\s*\n?", "\n", tail)
        for block in tail.split("\n\n"):
            block = block.strip()
            if not block:
                continue
            if len(block) <= 20 and not re.search(r"[。！？]", block):
                html_parts.append(
                    f'<h2 style="font-size:17px;font-weight:bold;color:#1a1a1a;'
                    f'border-left:3px solid #5a7a8a;padding-left:10px;margin:24px 0 12px 0;">'
                    f'{_html_escape(block)}</h2>'
                )
            else:
                html_parts.append(
                    f'<p style="margin:0 0 16px 0;text-indent:2em;">{_html_escape(block)}</p>'
                )

    # 分割线 + 尾注
    html_parts.append(
        '<hr style="border:none;border-top:1px solid #eee;margin:28px 0 16px 0;" />'
    )
    html_parts.append("</section>")
    return "".join(html_parts)


def _download_image_bytes(url: str) -> tuple[bytes, str]:
    if url.startswith("/writing/uploads/"):
        rel = unquote(url.removeprefix("/writing/uploads/"))
        path = os.path.realpath(os.path.join(config.DATA_DIR, rel))
        data_root = os.path.realpath(config.DATA_DIR)
        if not (path == data_root or path.startswith(data_root + os.sep)) or not os.path.exists(path):
            raise FileNotFoundError(url)
        with open(path, "rb") as fh:
            return fh.read(), os.path.basename(path)
    if url.startswith(("http://", "https://")):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; TN-Alpha/1.0)"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            filename = os.path.basename(resp.url.split("?", 1)[0]) or "image"
            return resp.read(), filename
    raise FileNotFoundError(url)


def _redirect(campaign_id: str | None = None) -> RedirectResponse:
    suffix = f"?campaign_id={campaign_id}" if campaign_id else ""
    return RedirectResponse(f"/schedule{suffix}", status_code=303)


def _safe_redirect(raw: str | None, fallback: str = "/schedule") -> RedirectResponse:
    target = raw if raw and raw.startswith("/schedule") else fallback
    return RedirectResponse(target, status_code=303)


def _library_url(status: str = "all", campaign_id: str = "all") -> str:
    params = {}
    if status and status != "all":
        params["status"] = status
    if campaign_id and campaign_id != "all":
        params["campaign_id"] = campaign_id
    return "/schedule/library" + (f"?{urlencode(params)}" if params else "")


def _parse_datetime(raw: str | None) -> datetime | None:
    if not raw or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw.strip())
    except ValueError as exc:
        raise HTTPException(400, "发布时间格式不正确") from exc


def _default_publish_datetime(slot: ScheduleSlot) -> str:
    if slot.published_at is not None:
        return slot.published_at.strftime("%Y-%m-%dT%H:%M")
    publish_time = (slot.publish_time or "").strip() or "09:00"
    return f"{slot.publish_date.isoformat()}T{publish_time}"


def _campaigns(session: Session, brand_id: int) -> list[Campaign]:
    return session.exec(select(Campaign).where(Campaign.brand_id == brand_id).order_by(Campaign.id)).all()


def _ctx(request: Request, session: Session, campaign_id_raw: str | None = None) -> dict:
    brand = schedule.first_brand(session)
    if brand is None:
        return {"request": request, "brand": None, "campaigns": [], "weeks": [],
                "week_slots": {}, "articles": [], "slot_articles": {},
                "slot_metrics": {}, "article_topics": {}, "campaign_name": {},
                "stats": {}, "library_counts": {}, "schedule_setting": schedule.get_schedule_settings(session),
                "selected_campaign": "all", "level": getattr(request.state, "level", 0)}
    campaigns = _campaigns(session, brand.id)
    campaign_name = {c.id: c.name for c in campaigns}
    weeks = schedule.all_weeks(session, brand.id)
    week_slots = {w.id: schedule.week_slots(session, w.id) for w in weeks if w.id is not None}
    topic_ids = set()
    slot_article_ids: set[int] = set()
    slot_ids: list[int] = []
    for slots in week_slots.values():
        topic_ids.update(slot.topic_id for slot in slots)
        slot_article_ids.update(slot.article_id for slot in slots)
        slot_ids.extend(slot.id for slot in slots if slot.id is not None)
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
        "weeks": list(reversed(weeks)),
        "week_slots": week_slots,
        "slot_metrics": schedule.metrics_for_slots(session, slot_ids),
        "slot_articles": slot_articles,
        "article_topics": article_topics,
        "campaign_name": campaign_name,
        "stats": schedule.overview_stats(session, brand.id),
        "library_counts": schedule.library_counts(session, brand.id, campaigns),
        "schedule_setting": schedule.get_schedule_settings(session),
        "selected_campaign": "all",
        "level": getattr(request.state, "level", 0),
    }


def _library_state(article: Article, active_slots: dict[int, ScheduleSlot]) -> str:
    slot = active_slots.get(article.id)
    if slot is None:
        return "可排期"
    return slot.status


def _library_articles_in_scope(articles: list[Article], scope: str, campaign_id: int | None) -> list[Article]:
    if scope == "brand":
        return [a for a in articles if a.campaign_id is None]
    if scope == "campaign":
        return [a for a in articles if a.campaign_id == campaign_id]
    return articles


def _library_articles_in_status(articles: list[Article], active_slots: dict[int, ScheduleSlot],
                                status: str) -> list[Article]:
    if status in ("", "all"):
        return articles
    return [a for a in articles if _library_state(a, active_slots) == status]


def _library_ctx(request: Request, session: Session, campaign_id_raw: str = "all",
                 status_raw: str = "all",
                 article_id: int | None = None) -> dict:
    brand = schedule.first_brand(session)
    if brand is None:
        return {"request": request, "brand": None, "campaigns": [], "articles": [],
                "article_topics": {}, "active_slots": {}, "weeks": [], "selected_article": None,
                "selected_topic": None, "selected_slot": None, "selected_campaign": "all",
                "campaign_name": {}, "library_counts": {}, "status_links": [], "scope_links": [],
                "active_status": "all", "level": getattr(request.state, "level", 0)}
    scope, cid = _scope_from_query(campaign_id_raw)
    active_status = status_raw if status_raw in {key for key, _label, _match in LIBRARY_TABS} else "all"
    campaigns = _campaigns(session, brand.id)
    campaign_name = {c.id: c.name for c in campaigns}
    all_articles = schedule.library_articles(session, brand.id, include_all_campaigns=True)
    active_slots = schedule.active_slots_for_articles(session, [a.id for a in all_articles if a.id is not None])
    scoped_articles = _library_articles_in_scope(all_articles, scope, cid)
    articles = _library_articles_in_status(scoped_articles, active_slots, active_status)
    topic_ids = {a.topic_id for a in articles}
    article_topics = {
        t.id: t for t in session.exec(select(Topic).where(Topic.id.in_(topic_ids))).all()
    } if topic_ids else {}
    selected_article = None
    if article_id is not None:
        selected_article = next((a for a in articles if a.id == article_id), None)
    if selected_article is None and articles:
        selected_article = articles[0]
    selected_topic = article_topics.get(selected_article.topic_id) if selected_article else None
    selected_slot = active_slots.get(selected_article.id) if selected_article else None
    if scope == "all":
        weeks = schedule.all_weeks(session, brand.id)
    else:
        weeks = schedule.all_weeks(session, brand.id)
    status_links = [
        (key, label, _library_url(key, campaign_id_raw or "all"),
         len(_library_articles_in_status(scoped_articles, active_slots, key)))
        for key, label, _match in LIBRARY_TABS
    ]
    scope_links = []
    scope_defs = [("all", "全部范围"), ("brand", "品牌常青")]
    scope_defs.extend((str(c.id), c.name) for c in campaigns)
    for scope_key, label in scope_defs:
        skind, scid = _scope_from_query(scope_key)
        in_scope = _library_articles_in_scope(all_articles, skind, scid)
        count = len(_library_articles_in_status(in_scope, active_slots, active_status))
        scope_links.append((scope_key, label, _library_url(active_status, scope_key), count))
    return {
        "request": request,
        "brand": brand,
        "campaigns": campaigns,
        "articles": articles,
        "article_topics": article_topics,
        "active_slots": active_slots,
        "weeks": weeks,
        "selected_article": selected_article,
        "selected_topic": selected_topic,
        "selected_slot": selected_slot,
        "selected_campaign": campaign_id_raw or "all",
        "active_status": active_status,
        "status_links": status_links,
        "scope_links": scope_links,
        "campaign_name": campaign_name,
        "library_counts": schedule.library_counts(session, brand.id, campaigns),
        "level": getattr(request.state, "level", 0),
    }


@router.get("/schedule")
def schedule_home(request: Request, campaign_id: str = "all", time_updated: int = 0,
                  session: Session = Depends(get_session)):
    ctx = _ctx(request, session, campaign_id)
    ctx["time_updated"] = bool(time_updated)
    return templates.TemplateResponse(request, "schedule/home.html", ctx)


@router.get("/schedule/library")
def schedule_library(request: Request, campaign_id: str = "all", status: str = "all",
                     article_id: int | None = None,
                     session: Session = Depends(get_session)):
    return templates.TemplateResponse(
        request, "schedule/library.html", _library_ctx(request, session, campaign_id, status, article_id)
    )


@router.get("/schedule/weeks/{week_id}/pick")
def pick_article_modal(week_id: int, request: Request, return_to: str = "/schedule",
                       session: Session = Depends(get_session)):
    auth.require_level(request, 1)
    week = session.get(ScheduleWeek, week_id)
    if week is None:
        raise HTTPException(404, "周不存在")
    brand = schedule.first_brand(session)
    campaigns = _campaigns(session, brand.id) if brand else []
    campaign_name = {c.id: c.name for c in campaigns}
    articles = schedule.all_schedulable_articles(session, week.brand_id)
    topic_ids = {article.topic_id for article in articles}
    article_topics = {
        t.id: t for t in session.exec(select(Topic).where(Topic.id.in_(topic_ids))).all()
    } if topic_ids else {}
    return templates.TemplateResponse(request, "schedule/_pick_article_modal.html", {
        "request": request,
        "week": week,
        "campaign_scope_name": "全部内容",
        "campaign_name": campaign_name,
        "articles": articles,
        "article_topics": article_topics,
        "return_to": return_to if return_to.startswith("/schedule") else "/schedule",
    })


@router.get("/schedule/slots/{slot_id}/publish-modal")
def publish_modal(slot_id: int, request: Request, return_to: str = "/schedule",
                  session: Session = Depends(get_session)):
    auth.require_level(request, 0)
    slot = session.get(ScheduleSlot, slot_id)
    if slot is None:
        raise HTTPException(404, "排期不存在")
    article = session.get(Article, slot.article_id)
    return templates.TemplateResponse(request, "schedule/_publish_modal.html", {
        "request": request,
        "slot": slot,
        "article": article,
        "recommended_platform": (article.platform if article else ""),
        "default_published_at": _default_publish_datetime(slot),
        "return_to": return_to if return_to.startswith("/schedule") else "/schedule",
    })


@router.get("/schedule/articles/{article_id}/assign-modal")
def assign_article_modal(article_id: int, request: Request, return_to: str = "/schedule/library",
                         session: Session = Depends(get_session)):
    auth.require_level(request, 1)
    article = session.get(Article, article_id)
    if article is None:
        raise HTTPException(404, "文章不存在")
    topic = session.get(Topic, article.topic_id)
    if topic is None:
        raise HTTPException(404, "选题不存在")
    weeks = schedule.all_weeks(session, topic.brand_id)
    week_options = [
        {
            "id": week.id,
            "label": f"{week.week_start.strftime('%m.%d')}-{week.week_end.strftime('%m.%d')}",
            "week_start": week.week_start.isoformat(),
            "week_end": week.week_end.isoformat(),
        }
        for week in weeks if week.id is not None
    ]
    return templates.TemplateResponse(request, "schedule/_assign_article_modal.html", {
        "request": request,
        "article": article,
        "topic": topic,
        "weeks": weeks,
        "week_options": week_options,
        "return_to": return_to if return_to.startswith("/schedule") else "/schedule/library",
    })


@router.post("/schedule/weeks/add")
def add_week(request: Request, campaign_id: str = Form("brand"),
             session: Session = Depends(get_session)):
    auth.require_level(request, 1)
    brand = schedule.first_brand(session)
    if brand is None:
        raise HTTPException(404, "还没有品牌")
    schedule.add_week(session, brand.id)
    return _redirect()


@router.post("/schedule/recommend")
def recommend_schedule(request: Request, campaign_id: str = Form("all"),
                       session: Session = Depends(get_session)):
    auth.require_level(request, 1)
    brand = schedule.first_brand(session)
    if brand is None:
        raise HTTPException(404, "还没有品牌")
    setting = schedule.get_schedule_settings(session)
    schedule.recommend_slots(session, brand.id, prompt=setting.recommend_prompt)
    return _redirect()


@router.post("/schedule/settings/recommend-prompt")
def save_recommend_prompt(request: Request, recommend_prompt: str = Form(""),
                          session: Session = Depends(get_session)):
    auth.require_level(request, 1)
    schedule.save_recommend_prompt(session, recommend_prompt)
    return _safe_redirect("/schedule")


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
             platform: str = Form(""), notes: str = Form(""), return_to: str = Form(""),
             session: Session = Depends(get_session)):
    auth.require_level(request, 1)
    try:
        schedule.add_slot(session, week_id, article_id, publish_date, publish_time, platform, notes)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _safe_redirect(return_to)


@router.post("/schedule/slots/{slot_id}/move")
def move_slot(slot_id: int, request: Request, publish_date: date = Form(...),
              publish_time: str = Form(""), return_to: str = Form(""),
              session: Session = Depends(get_session)):
    auth.require_level(request, 1)
    try:
        schedule.move_slot(session, slot_id, publish_date, publish_time)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _safe_redirect(return_to, "/schedule?time_updated=1")


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
                 return_to: str = Form(""),
                 session: Session = Depends(get_session)):
    auth.require_level(request, 0)
    parsed_at = _parse_datetime(published_at)
    try:
        schedule.publish_slot(session, slot_id, platform, published_url, parsed_at)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _safe_redirect(return_to)


@router.post("/schedule/slots/{slot_id}/unpublish")
def unpublish_slot(slot_id: int, request: Request, session: Session = Depends(get_session)):
    auth.require_level(request, 0)
    try:
        schedule.unpublish_slot(session, slot_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _redirect()


@router.post("/schedule/slots/{slot_id}/metrics")
def save_slot_metrics(slot_id: int, request: Request,
                      platform: str | None = Form(None), published_url: str | None = Form(None),
                      published_at: str | None = Form(None),
                      wechat_read: int = Form(0), wechat_like: int = Form(0),
                      wechat_share: int = Form(0), xhs_like: int = Form(0),
                      xhs_comment: int = Form(0), xhs_collect: int = Form(0),
                      notes: str = Form(""), session: Session = Depends(get_session)):
    auth.require_level(request, 0)
    parsed_at = _parse_datetime(published_at)
    try:
        schedule.save_metric(
            session,
            slot_id,
            platform=platform,
            published_url=published_url,
            published_at=parsed_at,
            wechat_read=wechat_read,
            wechat_like=wechat_like,
            wechat_share=wechat_share,
            xhs_like=xhs_like,
            xhs_comment=xhs_comment,
            xhs_collect=xhs_collect,
            notes=notes,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _redirect()


@router.get("/schedule/articles/{article_id}/preview")
def article_preview(article_id: int, request: Request, session: Session = Depends(get_session)):
    article = session.get(Article, article_id)
    if article is None:
        raise HTTPException(404, "文章不存在")
    topic = session.get(Topic, article.topic_id)
    images = _article_images(session, article)
    publish_body = _publish_body(article.body)
    copy_text = "\n\n".join(part for part in [article.title, publish_body] if part)
    body_segments = _body_segments_with_images(article.body, images)
    xhs = _format_xhs(article.title, article.body, images)
    wechat_html = _format_wechat(article.title, article.body, images)
    return templates.TemplateResponse(request, "schedule/_article_preview.html", {
        "request": request,
        "article": article,
        "topic": topic,
        "images": images,
        "publish_body": publish_body,
        "body_segments": body_segments,
        "copy_text": copy_text,
        "xhs": xhs,
        "wechat_html": wechat_html,
    })


@router.get("/schedule/articles/{article_id}/images.zip")
def download_article_images(article_id: int, session: Session = Depends(get_session)):
    article = session.get(Article, article_id)
    if article is None:
        raise HTTPException(404, "文章不存在")
    images = _article_images(session, article)
    if not images:
        raise HTTPException(404, "没有可下载图片")

    buffer = io.BytesIO()
    written = 0
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for idx, image in enumerate(images, start=1):
            try:
                data, filename = _download_image_bytes(image["url"])
            except Exception:
                continue
            ext = os.path.splitext(filename)[1] or ".jpg"
            zf.writestr(f"{idx:02d}{ext}", data)
            written += 1
    if written == 0:
        raise HTTPException(404, "图片文件不可下载")
    buffer.seek(0)
    safe_title = re.sub(r"[^A-Za-z0-9._-]+", "-", article.title).strip("-") or f"article-{article_id}"
    return Response(
        buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_title}-images.zip"'},
    )
