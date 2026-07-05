"""🔥 深度热点源——Perplexity Sonar（实时新闻 + 引用）。付费（$1/千次），默认不勾。

自包含：逻辑内联（urllib），不依赖 OpenClaw。key = config.PERPLEXITY_API_KEY（env），未配则不可用。
"""
from app.core import config
from app.core.sources import _http
from app.core.sources.base import SourceAdapter

_URL = "https://api.perplexity.ai/chat/completions"


def _key() -> str:
    """DB(模型配置页)优先，空则回退 config/env。"""
    from sqlmodel import Session
    from app.core import db, settings
    with Session(db.engine) as s:
        return settings.search_api_key(s, "perplexity")


def _parse(query: str, data: dict) -> list[dict]:
    choices = data.get("choices", [])
    if not choices:
        return []
    content = (choices[0].get("message", {}).get("content", "") or "").strip()
    cites = (data.get("citations", []) or [])[:5]
    out: list[dict] = []
    if content:
        out.append({"title": f"Sonar：{query}", "summary": content,
                    "url": cites[0] if cites else "", "source": "sonar"})
    for u in cites:
        out.append({"title": u, "summary": "", "url": u, "source": "sonar"})
    return out


class SonarAdapter(SourceAdapter):
    name = "sonar"
    label = "🔥 深度热点"
    emoji = "🔥"
    paid = True
    default_on = False

    def is_available(self) -> bool:
        return bool(_key())

    def search(self, query: str) -> list[dict]:
        key = _key()
        if not key:
            raise NotImplementedError("未配置 Perplexity API key（模型配置页填）")
        payload = {"model": "sonar", "messages": [{"role": "user", "content": query}],
                   "max_tokens": 1000, "return_citations": True}
        headers = {"Authorization": f"Bearer {key}"}
        data = _http.post_json(_URL, payload, headers, timeout=max(config.SOURCE_TIMEOUT, 60))
        return _parse(query, data)
