"""stub LLM provider：确定性假数据，无需密钥/网络。

- 文本：带 task 标记 + 输入指纹/摘要，便于测试断言与人工识别"这是假的"。
- 图像：返回稳定的占位路径。

同样的输入永远得到同样的输出（纯函数），方便测试。
"""
import hashlib
import re


def _fingerprint(prompt: str) -> str:
    return hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:8]


def generate_text(prompt: str, task: str = "default") -> str:
    prompt = prompt or ""
    if task == "topic_gen":
        m = re.search(r"生成\s+(\d+)\s+个", prompt)
        count = max(1, min(int(m.group(1)) if m else 3, 10))
        return "\n\n".join(
            "标题：占位选题{n}\n"
            "纲要：这是 stub provider 在真实模型不可用时生成的结构化占位选题，用于保证开发流程可跑通。请接入真实模型后重新生成。\n"
            "受众：城市青年\n"
            "时效：中\n"
            "素材：知识库素材\n"
            "配图：按品牌视觉风格生成\n"
            "时机：近期".format(n=i)
            for i in range(1, count + 1)
        )
    head = next((ln.strip() for ln in prompt.splitlines() if ln.strip()), "（空输入）")
    return (
        f"[stub:{task}] 已解析输入（{len(prompt)} 字，指纹 {_fingerprint(prompt)}）。\n"
        f"要点：{head[:60]}\n"
        f"（这是 stub 生成的占位结果，接入真实模型后自动替换。）"
    )


def generate_image(prompt: str) -> str:
    return f"/static/placeholder/{_fingerprint(prompt or '')}.png"
