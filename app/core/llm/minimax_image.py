"""MiniMax 图像 provider。

MiniMax M3 是文本模型；MiniMax 图像生成走同一 API Key/Base URL 下的
`/image_generation` endpoint，图像模型使用 `image-01`。

返回值为本地文件路径（已下载到 DATA_DIR/images/），与 codex provider 行为一致，
避免 OSS 临时 URL 过期导致图片失效。

批量生成：generate_images 用 API 的 n 参数一次出多张图（n 范围 1-9），
远比串行多次调用高效（n=4 耗时与 n=1 相同，约 20s）。
"""
import os
import uuid

import httpx

from app.core import config


def _download_to_local(url: str) -> str:
    """把远程图片下载到 DATA_DIR/images/，返回本地路径。"""
    out_dir = os.path.join(config.DATA_DIR, "images")
    os.makedirs(out_dir, exist_ok=True)
    ext = ".jpg"
    low = url.lower().split("?")[0]
    if low.endswith(".png"):
        ext = ".png"
    elif low.endswith(".jpeg") or low.endswith(".jpg"):
        ext = ".jpg"
    elif low.endswith(".webp"):
        ext = ".webp"
    path = os.path.join(out_dir, f"{uuid.uuid4().hex}{ext}")
    # trust_env=False 避免受 http_proxy 等环境变量影响（MiniMax 走直连）
    with httpx.Client(timeout=60, trust_env=False) as client:
        r = client.get(url)
        r.raise_for_status()
        with open(path, "wb") as fh:
            fh.write(r.content)
    return path


def generate_images(prompt: str, base_url: str, api_key: str, model: str = "image-01",
                    n: int = 4, timeout: int = 60) -> list[str]:
    """批量生成 n 张图，下载到本地，返回本地路径列表。n 范围 1-9（API 限制）。"""
    if not api_key:
        raise RuntimeError("MiniMax 图像 provider 未配置 api_key")
    n = max(1, min(n, 9))  # API 限制 n ∈ [1, 9]
    url = base_url.rstrip("/") + "/image_generation"
    # API 调用也走直连，避免代理变量干扰
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        resp = client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model or "image-01",
                "prompt": prompt,
                "aspect_ratio": "1:1",
                "response_format": "url",
                "n": n,
            },
        )
    resp.raise_for_status()
    data = resp.json()
    base_resp = data.get("base_resp") or {}
    if base_resp.get("status_code") not in (None, 0):
        raise RuntimeError(base_resp.get("status_msg") or "MiniMax 图像生成失败")
    urls = ((data.get("data") or {}).get("image_urls") or [])
    if not urls:
        raise RuntimeError("MiniMax 响应未含图片 URL")
    # 下载到本地，避免 OSS 临时 URL 过期失效
    paths: list[str] = []
    for u in urls:
        try:
            paths.append(_download_to_local(u))
        except Exception as e:
            # 下载失败时回退到远程 URL（有总比没有好），并打印警告
            print(f"[minimax_image] 图片下载失败，回退远程 URL：{e}")
            paths.append(u)
    return paths


def generate_image(prompt: str, base_url: str, api_key: str, model: str = "image-01",
                   timeout: int = 60) -> str:
    """单张生成，返回首个本地路径（兼容原契约）。"""
    return generate_images(prompt, base_url, api_key, model, n=1, timeout=timeout)[0]
