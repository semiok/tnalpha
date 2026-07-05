"""Google 搜索源——Gemini grounding（google_search 工具）返回 AI 综合答案 + 引用来源。

自包含：逻辑内联（urllib），不依赖 OpenClaw。key 从 DB(模型配置页)读、空则回退 config.GEMINI_API_KEY。
模型用 gemini-2.5-flash（新项目免费层在 2.5，2.0-flash 对新项目额度为 0）；关思考(thinkingBudget=0)提速。
"""
from app.core import config
from app.core.sources import _http
from app.core.sources.base import SourceAdapter

_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.5-flash:generateContent")


def _key() -> str:
    """DB(模型配置页)优先，空则回退 config/env。开短会话，adapter 自包含。"""
    from sqlmodel import Session
    from app.core import db, settings
    with Session(db.engine) as s:
        return settings.search_api_key(s, "gemini")


def _parse(query: str, data: dict) -> list[dict]:
    cands = data.get("candidates", [])
    if not cands:
        return []
    parts = cands[0].get("content", {}).get("parts", [])
    answer = "".join(p.get("text", "") for p in parts).strip()
    cites: list[dict] = []
    for ch in cands[0].get("groundingMetadata", {}).get("groundingChunks", []):
        w = ch.get("web", {})
        if w.get("uri"):
            cites.append({"title": w.get("title", ""), "url": w.get("uri", "")})
    out: list[dict] = []
    if answer:   # AI 综合答案是主命中（作 summary 喂选题 prompt）
        out.append({"title": f"Google：{query}", "summary": answer,
                    "url": cites[0]["url"] if cites else "", "source": "google"})
    for c in cites:   # 附引用来源
        out.append({"title": c["title"], "summary": "", "url": c["url"], "source": "google"})
    return out


class GoogleAdapter(SourceAdapter):
    name = "google"
    label = "Google 搜索"
    emoji = "🌐"
    paid = False
    default_on = True   # 品牌常青模式默认勾选（活动模式由前端按范围取消，见 topic/home.html topicGen）

    def is_available(self) -> bool:
        return bool(_key())

    def search(self, query: str) -> list[dict]:
        key = _key()
        if not key:
            raise NotImplementedError("未配置 Gemini API key（模型配置页填）")
        payload = {
            "contents": [{"parts": [{"text": query}]}],
            "tools": [{"google_search": {}}],
            "generationConfig": {
                "temperature": 0.1, "maxOutputTokens": 2048,
                "thinkingConfig": {"thinkingBudget": 0},   # 关思考：搜索摘要用不上，省时省钱
            },
        }
        data = _http.post_json(f"{_URL}?key={key}", payload, timeout=config.SOURCE_TIMEOUT)
        return _parse(query, data)
