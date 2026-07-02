"""搜索源注册表 + 统一入口。

模块调 `sources.search(source, query)`，不直接 curl 外部 API（铁律 §3）。
stub 先行返回假数据；真实源（热点/小红书/公众号/网络）已占位、接口已定，
未实现时按 `fallback` 回退 stub，保证端到端可跑。真实 adapter 接好后 `register` 覆盖即可。

    from app.core import sources
    hits = sources.search("stub", "国潮")          # → list[dict]
    hits = sources.search("hot", "国潮")            # 未接入 → 回退 stub
"""
from app.core.sources.base import SourceAdapter
from app.core.sources.stub import StubAdapter

_stub = StubAdapter()


class _NotReady(SourceAdapter):
    """真实源占位：接口已定，实现前 `search` 抛 NotImplementedError（上层可回退 stub）。"""

    def __init__(self, name: str):
        self.name = name

    def search(self, query: str) -> list[dict]:
        raise NotImplementedError(f"搜索源 '{self.name}' 尚未接入")


# 源标识 → adapter 实例。真实源占位待接。
_REGISTRY: dict[str, SourceAdapter] = {
    "stub": _stub,
    "hot": _NotReady("hot"),   # 热点
    "xhs": _NotReady("xhs"),   # 小红书
    "mp": _NotReady("mp"),     # 公众号
    "web": _NotReady("web"),   # 网络
}


def register(adapter: SourceAdapter) -> None:
    """注册/覆盖一个源（真实 adapter 接好后调用）。"""
    _REGISTRY[adapter.name] = adapter


def available() -> list[str]:
    return list(_REGISTRY)


def get(source: str) -> SourceAdapter:
    if source not in _REGISTRY:
        raise ValueError(f"未知搜索源: {source}")
    return _REGISTRY[source]


def search(source: str, query: str, fallback: bool = True) -> list[dict]:
    """检索指定源。源未接入且 fallback=True 时回退 stub，保证有结果可用。"""
    adapter = get(source)
    try:
        return adapter.search(query)
    except NotImplementedError:
        if fallback:
            return _stub.search(query)
        raise
