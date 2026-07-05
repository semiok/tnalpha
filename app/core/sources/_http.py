"""搜索源共用的极简 HTTP（stdlib urllib，零第三方依赖）。测试 monkeypatch 这两个函数即可。"""
import json
import urllib.request


def post_json(url: str, payload: dict, headers: dict | None = None, timeout: int = 30) -> dict:
    data = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def get_text(url: str, headers: dict | None = None, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        charset = r.headers.get_content_charset() or "utf-8"
        return r.read().decode(charset, "replace")
