"""MiniMax 图像 provider。

MiniMax M3 是文本模型；MiniMax 图像生成走同一 API Key/Base URL 下的
`/image_generation` endpoint，图像模型使用 `image-01`。返回值为图片 URL，
符合 generate_image 返回 str 的契约。
"""
import httpx

def generate_image(prompt: str, base_url: str, api_key: str, model: str = "image-01", timeout: int = 60) -> str:
    if not api_key:
        raise RuntimeError("MiniMax 图像 provider 未配置 api_key")
    url = base_url.rstrip("/") + "/image_generation"
    resp = httpx.post(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model or "image-01",
            "prompt": prompt,
            "aspect_ratio": "1:1",
            "response_format": "url",
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    base_resp = data.get("base_resp") or {}
    if base_resp.get("status_code") not in (None, 0):
        raise RuntimeError(base_resp.get("status_msg") or "MiniMax 图像生成失败")
    urls = ((data.get("data") or {}).get("image_urls") or [])
    if not urls:
        raise RuntimeError("MiniMax 响应未含图片 URL")
    return urls[0]
