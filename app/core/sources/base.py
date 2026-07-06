"""搜索源适配层的抽象接口（skill 式：每个源自描述 + 可发现）。

每个源（Google/搜狗公众号/深度热点/小红书…）= 一个 `SourceAdapter` 子类，
既声明**展示元数据**（label/emoji/是否付费/是否默认勾选/是否可用），又实现 `search`，
返回统一结构的结果。上层（②选题库）只认这个结构、并按 `catalog()` **动态渲染勾选框**，
不写死在模板里——加一个源=加一个 adapter 文件，UI 自动出现。

**自包含**：真实源的抓取逻辑内联在各 adapter 里（urllib/bs4），不依赖 OpenClaw 或本机脚本，
key 走 `core/config`（env）。未配 key/依赖缺失 → `is_available()` 为 False，UI 灰掉、批量搜索跳过。
"""
from abc import ABC, abstractmethod


class SourceAdapter(ABC):
    """搜索源适配器基类。子类实现 `search` + 覆盖展示元数据。

    统一结果结构（每条）：
        {"title": str, "summary": str, "url": str, "source": str}
    """

    #: 源标识，注册表 key（子类覆盖）
    name: str = "base"
    #: UI 显示名
    label: str = "基础源"
    #: UI 图标
    emoji: str = "🔍"
    #: 付费源？UI 标「付费」、默认不勾
    paid: bool = False
    #: 默认勾选？
    default_on: bool = False

    def is_available(self) -> bool:
        """依赖（API key / 库）是否就绪。为 False 时 UI 灰掉、批量搜索跳过。默认可用。"""
        return True

    @abstractmethod
    def search(self, query: str) -> list[dict]:
        """按 query 检索，返回统一结构结果列表。未接入的源可抛 NotImplementedError。"""
        raise NotImplementedError

    def meta(self) -> dict:
        """给 UI 的自描述（catalog 用）。"""
        return {
            "name": self.name, "label": self.label, "emoji": self.emoji,
            "paid": self.paid, "default_on": self.default_on,
            "enabled": self.is_available(),
        }
