"""MET-13 LLM provider 路由 + 「模型配置」页测试。

覆盖：
- openai_compat provider 打到正确 endpoint、无 key 报错
- 路由按 DB 设置分发（openai / claude-cli / codex），任何失败回退 stub
- 路由每次读 DB（改设置立即生效）
- 配置页权限（owner 进 / editor 403 / anon 跳登录）、持久化、api_key 打码不回显、留空/打码不覆盖
"""
import httpx
import pytest

from app.core import llm

# 路由分发用的假设置（绕过 DB）
_OPENAI = {"text_provider": "openai", "image_provider": "stub",
           "openai_base_url": "https://x/v1", "openai_api_key": "sk", "openai_model": "m",
           "claude_model": "sonnet"}
_CLAUDE = {**_OPENAI, "text_provider": "claude-cli", "claude_model": "opus"}
_CODEX = {**_OPENAI, "text_provider": "stub", "image_provider": "codex"}


# ── openai 兼容 provider 本体 ──

def test_openai_compat_calls_endpoint(monkeypatch):
    from app.core.llm import openai_compat
    seen = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "  真实回答  "}}]}

    def fake_post(url, headers, json, timeout):
        seen.update(url=url, auth=headers["Authorization"], model=json["model"])
        return FakeResp()

    monkeypatch.setattr(httpx, "post", fake_post)
    out = openai_compat.generate_text("你好", "https://api.deepseek.com/v1", "sk-abc", "deepseek-chat")
    assert out == "真实回答"                                   # 去空白 + 取 content
    assert seen["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-abc"
    assert seen["model"] == "deepseek-chat"


def test_openai_compat_without_key_raises():
    from app.core.llm import openai_compat
    with pytest.raises(RuntimeError):
        openai_compat.generate_text("x", "https://x/v1", "", "m")


# ── 路由分发（monkeypatch _settings 绕过 DB）──

def test_router_dispatches_to_openai(monkeypatch):
    monkeypatch.setattr(llm, "_settings", lambda: _OPENAI)
    seen = {}

    def fake(prompt, base_url, api_key, model, timeout=60):
        seen.update(base_url=base_url, api_key=api_key, model=model)
        return "OPENAI结果"

    monkeypatch.setattr(llm.openai_compat, "generate_text", fake)
    assert llm.generate_text("hi") == "OPENAI结果"
    # 参数顺序正确（base_url/api_key/model 没传反）
    assert seen == {"base_url": "https://x/v1", "api_key": "sk", "model": "m"}


def test_router_dispatches_to_claude_cli_with_model(monkeypatch):
    monkeypatch.setattr(llm, "_settings", lambda: _CLAUDE)
    seen = {}

    def fake(prompt, model="sonnet", timeout=180):
        seen["model"] = model
        return "CLAUDE结果"

    monkeypatch.setattr(llm.claude_cli, "generate_text", fake)
    assert llm.generate_text("hi") == "CLAUDE结果"
    assert seen["model"] == "opus"                            # 用 DB 里配的 claude_model


def test_router_text_falls_back_to_stub_on_error(monkeypatch):
    monkeypatch.setattr(llm, "_settings", lambda: _OPENAI)

    def boom(*a, **k):
        raise RuntimeError("网络炸了")

    monkeypatch.setattr(llm.openai_compat, "generate_text", boom)
    out = llm.generate_text("hi", task="brand_digest")
    assert "[stub:brand_digest]" in out                       # 端到端不崩，退 stub


def test_router_image_falls_back_to_stub_on_error(monkeypatch):
    monkeypatch.setattr(llm, "_settings", lambda: _CODEX)

    def boom(*a, **k):
        raise RuntimeError("codex 未授权")

    monkeypatch.setattr(llm.codex_image, "generate_image", boom)
    out = llm.generate_image("敦煌飞天")
    assert out.endswith(".png") and "placeholder" in out


def test_router_unconfigured_uses_stub(monkeypatch):
    monkeypatch.setattr(llm, "_settings", lambda: {**_OPENAI, "text_provider": "stub"})
    assert "[stub:default]" in llm.generate_text("hi")


# ── 路由每次读 DB（fresh_db 已把 db.engine 指到测试库）──

def test_settings_read_from_db(fresh_db):
    from sqlmodel import Session
    from app.core.settings import get_llm_settings
    with Session(fresh_db) as s:
        st = get_llm_settings(s)
        st.text_provider = "openai"
        st.openai_api_key = "sk-live"
        st.openai_model = "gpt-x"
        s.add(st)
        s.commit()
    got = llm._settings()                                     # 路由动态读 db.engine
    assert got["text_provider"] == "openai"
    assert got["openai_api_key"] == "sk-live"
    assert got["openai_model"] == "gpt-x"


# ── 配置页：权限 ──

def test_page_owner_ok(owner_client):
    r = owner_client.get("/settings/llm")
    assert r.status_code == 200
    assert "模型配置" in r.text
    # 三种模式齐全
    assert "claude-cli" in r.text and "codex" in r.text and "其他 API" in r.text


def test_page_editor_forbidden(editor_client):
    assert editor_client.get("/settings/llm").status_code == 403


def test_page_anon_redirected_to_login(anon_client):
    r = anon_client.get("/settings/llm", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/login")


def test_editor_cannot_post(editor_client):
    assert editor_client.post("/settings/llm", data={"text_provider": "stub"}).status_code == 403


# ── 配置页：持久化 + api_key 打码 / 不覆盖 ──

def _post(client, **over):
    data = {"text_provider": "openai", "image_provider": "stub",
            "openai_base_url": "https://x/v1", "openai_api_key": "sk-secret",
            "openai_model": "m", "claude_model": "sonnet"}
    data.update(over)
    return client.post("/settings/llm", data=data, follow_redirects=False)


def test_post_persists_and_masks_key(owner_client):
    r = _post(owner_client, openai_base_url="https://api.deepseek.com/v1",
              openai_api_key="sk-secret-1234", openai_model="deepseek-chat")
    assert r.status_code == 303
    page = owner_client.get("/settings/llm").text
    assert "https://api.deepseek.com/v1" in page and "deepseek-chat" in page
    assert "sk-secret-1234" not in page                       # 明文绝不回显
    assert "••••••1234" in page                               # 打码显示尾 4 位


def test_empty_key_keeps_existing(owner_client):
    _post(owner_client, openai_api_key="sk-keepme-9999", openai_model="m1")
    _post(owner_client, openai_api_key="", openai_model="m2")  # 留空 key，改 model
    got = llm._settings()
    assert got["openai_api_key"] == "sk-keepme-9999"          # key 保留
    assert got["openai_model"] == "m2"                        # 其他字段照常更新


def test_masked_key_not_overwrite(owner_client):
    _post(owner_client, openai_api_key="sk-original-7777")
    _post(owner_client, openai_api_key="••••••7777")          # 回填打码值（用户没改 key）
    assert llm._settings()["openai_api_key"] == "sk-original-7777"
