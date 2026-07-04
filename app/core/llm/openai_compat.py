"""OpenAI 兼容文本 provider——覆盖 OpenAI / DeepSeek / Moonshot / MiniMax / Ollama / cc-proxy 等。

走流式（SSE）累积 chunk：对长生成更稳（非流式经代理易网关 502 超时），
所有 OpenAI 兼容端点都支持。只要目标提供 `{base_url}/chat/completions` 即可用。
"""
import json

import httpx


def generate_text(prompt: str, base_url: str, api_key: str,
                  model: str, timeout: int = 60) -> str:
    if not api_key:
        raise RuntimeError("openai 兼容 provider 未配置 api_key")
    url = base_url.rstrip("/") + "/chat/completions"
    body = {"model": model, "stream": True,
            "messages": [{"role": "user", "content": prompt}]}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    parts: list[str] = []
    with httpx.stream("POST", url, headers=headers, json=body, timeout=timeout) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                ev = json.loads(data)
            except json.JSONDecodeError:
                continue
            delta = ((ev.get("choices") or [{}])[0].get("delta") or {}).get("content")
            if delta:
                parts.append(delta)
    text = "".join(parts).strip()
    if not text:
        raise RuntimeError("openai 兼容 provider 返回空")
    return text
