"""MiniMax 图像 provider。

MiniMax M3 是文本模型；MiniMax 图像生成走同一 API Key/Base URL 下的
`/image_generation` endpoint，图像模型使用 `image-01`。返回值为图片 URL，
符合 generate_image 返回 str 的契约。

批量生成：generate_images 用 API 的 n 参数一次出多张图（n 范围 1-9），
远比串行多次调用高效（n=4 耗时与 n=1 相同，约 20s）。
"""
import httpx


def generate_images(prompt: str, base_url: str, api_key: str, model: str = "image-01",
                    n: int = 4, timeout: int = 60) -> list[str]:
    """批量生成 n 张图，返回 URL 列表。n 范围 1-9（API 限制）。"""
    if not api_key:
        raise RuntimeError("MiniMax 图像 provider 未配置 api_key")
    n = max(1, min(n, 9))  # API 限制 n ∈ [1, 9]
    url = base_url.rstrip("/") + "/image_generation"
    resp = httpx.post(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model or "image-01",
            "prompt": prompt,
            "aspect_ratio": "1:1",
            "response_format": "url",
            "n": n,
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
    return urls


def generate_image(prompt: str, base_url: str, api_key: str, model: str = "image-01",
                   timeout: int = 60) -> str:
    """单张生成，返回首个 URL（兼容原契约）。"""
    return generate_images(prompt, base_url, api_key, model, n=1, timeout=timeout)[0]
