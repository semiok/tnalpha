"""stub LLM provider：确定性假数据，无需密钥/网络。

- 文本：带 task 标记 + 输入指纹/摘要，便于测试断言与人工识别"这是假的"。
- 图像：返回稳定的占位路径。

同样的输入永远得到同样的输出（纯函数），方便测试。
"""
import hashlib


def _fingerprint(prompt: str) -> str:
    return hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:8]


def generate_text(prompt: str, task: str = "default") -> str:
    prompt = prompt or ""
    head = next((ln.strip() for ln in prompt.splitlines() if ln.strip()), "（空输入）")
    return (
        f"[stub:{task}] 已解析输入（{len(prompt)} 字，指纹 {_fingerprint(prompt)}）。\n"
        f"要点：{head[:60]}\n"
        f"（这是 stub 生成的占位结果，接入真实模型后自动替换。）"
    )


def generate_image(prompt: str) -> str:
    return f"/static/placeholder/{_fingerprint(prompt or '')}.png"
