"""Google 搜索源——Gemini grounding（google_search 工具）返回 AI 综合答案 + 引用来源。

自包含：逻辑内联（urllib），不依赖 OpenClaw。key = config.GEMINI_API_KEY（env），未配则不可用。
"""
from app.core import config
from app.core.sources import _http
from app.core.sources.base import SourceAdapter

_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.0-flash:generateContent")


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
    default_on = True

    def is_available(self) -> bool:
        return bool(config.GEMINI_API_KEY)

    def search(self, query: str) -> list[dict]:
        if not self.is_available():
            raise NotImplementedError("未配置 GEMINI_API_KEY")
        payload = {
            "contents": [{"parts": [{"text": query}]}],
            "tools": [{"google_search": {}}],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 2048},
        }
        data = _http.post_json(f"{_URL}?key={config.GEMINI_API_KEY}", payload,
                               timeout=config.SOURCE_TIMEOUT)
        return _parse(query, data)
