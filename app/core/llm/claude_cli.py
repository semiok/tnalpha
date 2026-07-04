"""Claude 授权 provider——走本机 claude CLI（headless -p），用 Max 订阅、零 API 费。

复用 tngen 的容错：claude 的 SessionEnd hook 在精简环境（launchd）下可能让进程 rc≠0，
但正文已在 stdout 生成——此时用 stdout，不丢结果。
"""
import subprocess

from app.core import config


def generate_text(prompt: str, model: str = "sonnet", timeout: int = 180,
                  pdf_path: str | None = None) -> str:
    if pdf_path:  # 深度读图：让 claude 读该 PDF（含图片页），用 Read 工具（照 tngen）
        args = [config.CLAUDE_BIN, "-p", f"{prompt}\n\n请阅读该 PDF 文件（含图片页）：{pdf_path}",
                "--model", model, "--allowedTools", "Read"]
    else:
        args = [config.CLAUDE_BIN, "-p", prompt, "--model", model]
    try:
        # stdin=DEVNULL：headless 必须，否则 claude 等 stdin 会进交互/批准模式
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"claude 调用超时（>{timeout}s）") from e
    out = (r.stdout or "").strip()
    # claude 认证失败/API 错误会把错误文本当"回答"打到 stdout（rc 可能为 0）——
    # 视为失败并 raise（让上层回退 stub），否则会把 "Failed to authenticate..." 当解读存库。
    if out and ("Failed to authenticate" in out or "authentication_error" in out
                or out.startswith("API Error") or '"type":"error"' in out):
        raise RuntimeError(f"claude 输出为错误：{out[:150]}")
    if r.returncode != 0:
        if out:                       # rc≠0 但正文已生成（hook 清理失败）→ 用 stdout
            return out
        raise RuntimeError(f"claude 调用失败(rc={r.returncode}): {(r.stderr or '')[:200]}")
    return out
