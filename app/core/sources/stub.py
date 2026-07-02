"""stub 搜索源：返回确定性假热点/笔记，保证 ② 选题等模块端到端可跑。"""
from app.core.sources.base import SourceAdapter


class StubAdapter(SourceAdapter):
    name = "stub"

    def search(self, query: str) -> list[dict]:
        query = (query or "").strip() or "示例"
        return [
            {
                "title": f"{query} 相关热点 {i}",
                "summary": f"这是关于「{query}」的 stub 结果 {i}，接入真实源后替换。",
                "url": f"https://example.com/{query}/{i}",
                "source": self.name,
            }
            for i in range(1, 4)
        ]
