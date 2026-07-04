"""Codex 授权 provider（文本）——Codex 订阅 OAuth → Responses API 生成文本，零 API 费。

与 codex_image 同一套鉴权（读本机 ~/.codex/auth.json 的 access_token，走流式 Responses API），
但不带 image_generation 工具、只累积文本增量（response.output_text.delta）。
pdf_path 非空=深度读图：把 PDF（含图片页）作 input_file(base64) 随请求发，gpt-5.5 直接读。

单次请求抽成 _attempt；外层自动重试瞬时错误（OpenAI 偶发 "An error occurred..."），
授权类错误（401/403）不重试直接抛。全部重试用尽仍失败 → 抛，交由上层 router 回退 stub。
"""
import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from app.core import config

_MAX_ATTEMPTS = 3          # 首次 + 2 次重试
_BACKOFF_BASE = 1.5        # 递增退避基数（秒）
_IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "gif"}   # 走 input_image；其余（pdf）走 input_file


class _CodexAuthError(RuntimeError):
    """授权/认证类错误——重试无用，直接抛。"""


def _access(auth_path: str) -> tuple[str, str]:
    tok = json.loads(Path(auth_path).expanduser().read_text())["tokens"]
    return tok["access_token"], tok["account_id"]


def _file_part(fp: str) -> dict | None:
    """把一个文件转成 Responses API content 块：图片→input_image，PDF/其余→input_file。不存在则跳过。"""
    p = Path(fp)
    if not p.exists():
        return None
    ext = p.suffix.lower().lstrip(".")
    b64 = base64.b64encode(p.read_bytes()).decode()
    if ext in _IMAGE_EXTS:
        mime = "jpeg" if ext == "jpg" else ext
        return {"type": "input_image", "image_url": f"data:image/{mime};base64,{b64}"}
    return {"type": "input_file", "filename": p.name,
            "file_data": f"data:application/pdf;base64,{b64}"}


def _attempt(prompt: str, model: str | None, timeout: int, reasoning: str | None,
             pdf_path: str | None, attachments: list[str] | None) -> str:
    access_token, account_id = _access(config.CODEX_AUTH_PATH)
    content: list[dict] = [{"type": "input_text", "text": prompt}]
    files = list(attachments or [])
    if pdf_path:                               # 兼容旧签名：单 PDF 也当附件
        files.append(pdf_path)
    for fp in files:                           # 深度读图：PDF/图片直接交给 gpt-5.5 读
        part = _file_part(fp)
        if part:
            content.append(part)
    body = {
        "model": model or config.CODEX_TEXT_MODEL, "store": False, "stream": True,
        "instructions": "You are a helpful assistant. 用简体中文回答。",
        "input": [{"role": "user", "content": content}],
        # 思考模式：gpt-5.5 支持 reasoning effort，默认 medium（速度/质量平衡；env 可调 high/low）
        "reasoning": {"effort": reasoning or config.CODEX_REASONING_EFFORT},
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
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        detail = e.read()[:200].decode("utf-8", "replace")
        if e.code in (401, 403):      # 授权失败：重试无用
            raise _CodexAuthError(f"Codex 授权失败 HTTP {e.code}: {detail}") from e
        raise RuntimeError(f"Codex 文本 HTTP {e.code}: {detail}") from e

    parts: list[str] = []
    done_text = ""
    with resp:                              # 流式连接用完即关，避免 socket 泄漏
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
            t = ev.get("type", "")
            if t in ("response.failed", "error"):
                msg = (ev.get("error") or {}).get("message") or ev.get("message") or "未知错误"
                raise RuntimeError(f"Codex 文本生成失败: {msg}")
            if t == "response.output_text.delta":
                parts.append(ev.get("delta", ""))
            elif t == "response.output_text.done":
                done_text = ev.get("text", "") or done_text
    out = ("".join(parts) or done_text).strip()
    if not out:
        raise RuntimeError("Codex 响应未含文本")
    return out


def generate_text(prompt: str, model: str | None = None, timeout: int = 180,
                  reasoning: str | None = None, pdf_path: str | None = None,
                  attachments: list[str] | None = None) -> str:
    last: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            return _attempt(prompt, model, timeout, reasoning, pdf_path, attachments)
        except _CodexAuthError:           # 授权错误：不重试
            raise
        except RuntimeError as e:         # 瞬时错误（HTTP 5xx/limit、response.failed、空响应）：重试
            last = e
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_BACKOFF_BASE * (attempt + 1))
    raise last
