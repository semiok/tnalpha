"""OpenAI 兼容文本 provider——覆盖 OpenAI / DeepSeek / Moonshot / MiniMax / Ollama / cc-proxy 等。

走流式（SSE）累积 chunk：对长生成更稳（非流式经代理易网关 502 超时），
所有 OpenAI 兼容端点都支持。只要目标提供 `{base_url}/chat/completions` 即可用。
"""
import json
import time

import httpx


def stream_text(prompt: str, base_url: str, api_key: str,
                model: str, timeout: int = 60):
    """以增量文本迭代 OpenAI 兼容接口的输出。"""
    if not api_key:
        raise RuntimeError("openai 兼容 provider 未配置 api_key")
    url = base_url.rstrip("/") + "/chat/completions"
    body = {"model": model, "stream": True,
            "messages": [{"role": "user", "content": prompt}]}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    with httpx.stream("POST", url, headers=headers, json=body, timeout=timeout) as resp:
        resp.raise_for_status()
        emitted = False
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
                emitted = True
                yield delta
        if not emitted:
            raise RuntimeError("openai 兼容 provider 返回空")


def generate_text(prompt: str, base_url: str, api_key: str,
                  model: str, timeout: int = 60) -> str:
    """累积流式输出；保留原有同步调用接口。"""
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            return "".join(stream_text(prompt, base_url, api_key, model, timeout)).strip()
        except httpx.HTTPStatusError as exc:
            last_error = exc
            if exc.response.status_code < 500 or attempt == 4:
                raise
        except (httpx.TimeoutException, httpx.NetworkError, RuntimeError) as exc:
            last_error = exc
            if attempt == 4:
                raise
        time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"openai 兼容 provider 调用失败：{last_error}")
