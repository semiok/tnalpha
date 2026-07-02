"""搜索源适配层的抽象接口。

每个源（热点/小红书/公众号/网络/…）实现一个 `SourceAdapter` 子类，
返回统一结构的结果，上层（②选题库等）只认这个结构，不关心源怎么抓。
"""
from abc import ABC, abstractmethod


class SourceAdapter(ABC):
    """搜索源适配器基类。子类实现 `search`，返回统一 dict 列表。

    统一结果结构（每条）：
        {"title": str, "summary": str, "url": str, "source": str}
    """

    #: 源标识，用于注册表 key（子类覆盖）
    name: str = "base"

    @abstractmethod
    def search(self, query: str) -> list[dict]:
        """按 query 检索，返回统一结构结果列表。未接入的真实源可抛 NotImplementedError。"""
        raise NotImplementedError
