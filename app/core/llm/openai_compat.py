"""OpenAI 兼容文本 provider——覆盖 OpenAI / DeepSeek / Moonshot / MiniMax / Ollama 等。

只要目标提供 `{base_url}/chat/completions`（OpenAI 兼容），填 base_url + api_key + model 即可用。
"""
import httpx


def generate_text(prompt: str, base_url: str, api_key: str,
                  model: str, timeout: int = 60) -> str:
    if not api_key:
        raise RuntimeError("openai 兼容 provider 未配置 api_key")
    url = base_url.rstrip("/") + "/chat/completions"
    resp = httpx.post(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": model, "messages": [{"role": "user", "content": prompt}]},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()
