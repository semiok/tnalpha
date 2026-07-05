"""搜索源注册表 + 统一入口（skill 式：自描述 + 可发现）。

模块调 `sources.search(source, query)` 或 `sources.gather(names, query)`，不直接 curl 外部 API（铁律 §3）。
所有真实源**自包含在 tnalpha 内**（urllib/bs4，key 走 config env），不依赖 OpenClaw。

    from app.core import sources
    sources.catalog()                      # [{name,label,emoji,paid,default_on,enabled}, ...] → 渲染勾选框
    sources.gather(["google","mp"], "敦煌") # 跑多个源、合并命中，跳过未接入/报错的源
    sources.search("stub", "国潮")          # 单源；未接入且 fallback=True 时回退 stub
"""
from app.core.sources.base import SourceAdapter
from app.core.sources.gemini import GoogleAdapter
from app.core.sources.sogou import SogouAdapter
from app.core.sources.sonar import SonarAdapter
from app.core.sources.stub import StubAdapter

_stub = StubAdapter()


class _NotReady(SourceAdapter):
    """占位源：接口已定、实现前不可用（is_available=False，UI 灰掉）。"""

    def __init__(self, name: str, label: str, emoji: str):
        self.name, self.label, self.emoji = name, label, emoji

    def is_available(self) -> bool:
        return False

    def search(self, query: str) -> list[dict]:
        raise NotImplementedError(f"搜索源 '{self.name}' 尚未接入")


# 注册顺序 = UI 勾选框顺序：Google(默认勾) → 搜狗公众号 → 深度热点(付费) → 小红书(占位)
_REGISTRY: dict[str, SourceAdapter] = {}


def register(adapter: SourceAdapter) -> None:
    """注册/覆盖一个源。"""
    _REGISTRY[adapter.name] = adapter


for _a in (
    _stub,
    GoogleAdapter(),
    SogouAdapter(),
    SonarAdapter(),
    _NotReady("xhs", "小红书", "📕"),
):
    register(_a)


def available() -> list[str]:
    return list(_REGISTRY)


def get(source: str) -> SourceAdapter:
    if source not in _REGISTRY:
        raise ValueError(f"未知搜索源: {source}")
    return _REGISTRY[source]


def catalog(include_stub: bool = False) -> list[dict]:
    """给 UI 的源清单（自描述）。默认隐藏 stub（仅测试/回退用）。"""
    return [a.meta() for name, a in _REGISTRY.items()
            if include_stub or name != "stub"]


def search(source: str, query: str, fallback: bool = True) -> list[dict]:
    """检索指定源。源未接入且 fallback=True 时回退 stub，保证有结果可用。"""
    adapter = get(source)
    try:
        return adapter.search(query)
    except NotImplementedError:
        if fallback:
            return _stub.search(query)
        raise


def gather(names: list[str], query: str, per_source: int = 3) -> list[dict]:
    """跑多个源、合并命中（每源截 per_source）。跳过未知/未接入/报错的源，绝不抛。

    ②选题库批量搜索用：某个源（如搜狗抓取）挂了不影响其他源与整体生成。
    """
    query = (query or "").strip()
    if not query or not names:
        return []
    hits: list[dict] = []
    for n in names:
        try:
            adapter = get(n)
        except ValueError:
            continue
        if not adapter.is_available():
            continue
        try:
            res = adapter.search(query) or []
        except Exception:
            continue   # 单源失败（网络/反爬/解析）→ 跳过，不拖垮整体
        hits.extend(res[:per_source])
    return hits
