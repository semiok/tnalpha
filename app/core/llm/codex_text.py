"""Codex 授权 provider（文本）——Codex 订阅 OAuth → Responses API 生成文本，零 API 费。

与 codex_image 同一套鉴权（读本机 ~/.codex/auth.json 的 access_token，走流式 Responses API），
但不带 image_generation 工具、只累积文本增量（response.output_text.delta）。

任何失败（HTTP/鉴权/空响应）都 raise，交由上层 router 回退 stub，保证端到端不崩。
"""
import base64
import json
import urllib.error
import urllib.request
from pathlib import Path

from app.core import config


def _access(auth_path: str) -> tuple[str, str]:
    tok = json.loads(Path(auth_path).expanduser().read_text())["tokens"]
    return tok["access_token"], tok["account_id"]


def generate_text(prompt: str, model: str | None = None, timeout: int = 180,
                  reasoning: str | None = None, pdf_path: str | None = None) -> str:
    access_token, account_id = _access(config.CODEX_AUTH_PATH)
    content: list[dict] = [{"type": "input_text", "text": prompt}]
    if pdf_path:  # 深度读图：把 PDF（含图片页）作为 input_file 塞进去，gpt-5.5 直接读
        p = Path(pdf_path)
        b64 = base64.b64encode(p.read_bytes()).decode()
        content.append({"type": "input_file", "filename": p.name,
                        "file_data": f"data:application/pdf;base64,{b64}"})
    body = {
        "model": model or config.CODEX_TEXT_MODEL, "store": False, "stream": True,
        "instructions": "You are a helpful assistant. 用简体中文回答。",
        "input": [{"role": "user", "content": content}],
        # 思考模式：gpt-5.5 支持 reasoning effort，默认 high（深度解析质量优先）
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
        raise RuntimeError(f"Codex 文本 HTTP {e.code}: "
                           f"{e.read()[:200].decode('utf-8', 'replace')}") from e

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
