"""Codex 授权 provider——Codex 订阅 OAuth → Responses API image_generation 出图。

复用 tngen 逻辑：读本机 ~/.codex/auth.json 的 access_token，走流式 Responses API，
拿到 base64 图 → 落盘到 DATA_DIR/images → 返回路径（保持 generate_image 返回 str 路径的契约）。
"""
import base64
import json
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from app.core import config


def generate_image(prompt: str) -> str:
    b64 = _call_codex(prompt)
    out_dir = os.path.join(config.DATA_DIR, "images")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{uuid.uuid4().hex}.{config.IMAGE_FORMAT}")
    with open(path, "wb") as fh:
        fh.write(base64.b64decode(b64))
    return path


def _access(auth_path: str) -> tuple[str, str]:
    tok = json.loads(Path(auth_path).expanduser().read_text())["tokens"]
    return tok["access_token"], tok["account_id"]


def _call_codex(prompt: str) -> str:
    access_token, account_id = _access(config.CODEX_AUTH_PATH)
    body = {
        "model": config.CODEX_ENVELOPE_MODEL, "store": False, "stream": True,
        "instructions": "You are an image generation assistant.",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
        "tools": [{"type": "image_generation", "model": config.IMAGE_MODEL,
                   "size": config.IMAGE_SIZE, "quality": config.IMAGE_QUALITY,
                   "output_format": config.IMAGE_FORMAT}],
    }
    headers = {
        "Authorization": f"Bearer {access_token}", "ChatGPT-Account-Id": account_id,
        "Content-Type": "application/json", "Accept": "text/event-stream",
        "originator": "openclaw", "version": config.CODEX_CLIENT_VERSION,
        "User-Agent": f"openclaw/{config.CODEX_CLIENT_VERSION}",
    }
    req = urllib.request.Request(config.CODEX_RESPONSES_URL,
                                 data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=config.IMAGE_TIMEOUT)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Codex 图像 HTTP {e.code}: "
                           f"{e.read()[:200].decode('utf-8', 'replace')}") from e
    b64 = None
    with resp:                              # 确保流式连接用完即关，避免 socket 泄漏
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                ev = json.loads(data)
            except json.JSONDecodeError:
                continue
            if ev.get("type") in ("response.failed", "error"):
                msg = (ev.get("error") or {}).get("message") or ev.get("message") or "未知错误"
                raise RuntimeError(f"Codex 图像生成失败: {msg}")
            item = ev.get("item") or {}
            if (ev.get("type") == "response.output_item.done"
                    and item.get("type") == "image_generation_call" and item.get("result")):
                b64 = item["result"]
    if not b64:
        raise RuntimeError("Codex 响应未含图像数据")
    return b64
