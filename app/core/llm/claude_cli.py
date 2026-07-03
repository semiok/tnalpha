"""Claude 授权 provider——走本机 claude CLI（headless -p），用 Max 订阅、零 API 费。

复用 tngen 的容错：claude 的 SessionEnd hook 在精简环境（launchd）下可能让进程 rc≠0，
但正文已在 stdout 生成——此时用 stdout，不丢结果。
"""
import subprocess


def generate_text(prompt: str, model: str = "sonnet", timeout: int = 180) -> str:
    args = ["claude", "-p", prompt, "--model", model]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"claude 调用超时（>{timeout}s）") from e
    out = (r.stdout or "").strip()
    if r.returncode != 0:
        if out:                       # rc≠0 但正文已生成（hook 清理失败）→ 用 stdout
            return out
        raise RuntimeError(f"claude 调用失败(rc={r.returncode}): {(r.stderr or '')[:200]}")
    return out
