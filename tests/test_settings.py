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
           "image_base_url": "https://img/v1", "image_api_key": "img-sk", "image_model": "image-01",
           "claude_model": "sonnet"}
_MINIMAX = {
    **_OPENAI,
    "text_provider": "minimax-m3",
    "openai_base_url": "https://api.minimax.chat/v1",
    "openai_model": "MiniMax-M3",
}
_CLAUDE = {**_OPENAI, "text_provider": "claude-cli", "claude_model": "opus"}
_CODEX = {**_OPENAI, "text_provider": "stub", "image_provider": "codex"}
_MINIMAX_IMAGE = {**_MINIMAX, "text_provider": "stub", "image_provider": "minimax-m3"}


# ── openai 兼容 provider 本体 ──

def test_openai_compat_calls_endpoint(monkeypatch):
    from app.core.llm import openai_compat
    seen = {}

    class FakeStream:
        def __init__(self, headers, json):
            seen.update(auth=headers["Authorization"], model=json["model"],
                        stream=json.get("stream"))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"真实"}}]}'
            yield ""                                            # SSE 空行
            yield 'data: {"choices":[{"delta":{"content":"回答"}}]}'
            yield "data: [DONE]"

    def fake_stream(method, url, headers, json, timeout):
        seen["url"] = url
        return FakeStream(headers, json)

    monkeypatch.setattr(httpx, "stream", fake_stream)
    out = openai_compat.generate_text("你好", "https://api.deepseek.com/v1", "sk-abc", "deepseek-chat")
    assert out == "真实回答"                                   # 累积 SSE delta
    assert seen["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-abc"
    assert seen["model"] == "deepseek-chat"
    assert seen["stream"] is True                             # 流式


def test_openai_compat_without_key_raises():
    from app.core.llm import openai_compat
    with pytest.raises(RuntimeError):
        openai_compat.generate_text("x", "https://x/v1", "", "m")


# ── 路由分发（monkeypatch _settings 绕过 DB）──

def test_router_dispatches_to_openai(monkeypatch):
    monkeypatch.setattr(llm, "_settings", lambda *a, **k: _OPENAI)
    seen = {}

    def fake(prompt, base_url, api_key, model, timeout=60):
        seen.update(base_url=base_url, api_key=api_key, model=model)
        return "OPENAI结果"

    monkeypatch.setattr(llm.openai_compat, "generate_text", fake)
    assert llm.generate_text("hi") == "OPENAI结果"
    # 参数顺序正确（base_url/api_key/model 没传反）
    assert seen == {"base_url": "https://x/v1", "api_key": "sk", "model": "m"}


def test_router_dispatches_minimax_m3_as_openai_compatible(monkeypatch):
    monkeypatch.setattr(llm, "_settings", lambda *a, **k: _MINIMAX)
    seen = {}

    def fake(prompt, base_url, api_key, model, timeout=60):
        seen.update(base_url=base_url, api_key=api_key, model=model)
        return "MINIMAX结果"

    monkeypatch.setattr(llm.openai_compat, "generate_text", fake)
    assert llm.generate_text("hi") == "MINIMAX结果"
    assert seen == {
        "base_url": "https://api.minimax.chat/v1",
        "api_key": "sk",
        "model": "MiniMax-M3",
    }


def test_claude_cli_uses_configured_bin(monkeypatch):
    from app.core import config
    from app.core.llm import claude_cli

    monkeypatch.setattr(config, "CLAUDE_BIN", "/custom/path/claude")
    seen = {}

    class _R:
        returncode = 0
        stdout = "OK"
        stderr = ""

    def fake_run(args, **kw):
        seen["bin"] = args[0]
        return _R()

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    assert claude_cli.generate_text("hi") == "OK"
    assert seen["bin"] == "/custom/path/claude"   # 用可配置 CLAUDE_BIN，不写死 "claude"


def test_claude_cli_auth_error_raises(monkeypatch):
    """claude 401 把错误文本打到 stdout（rc=0）→ 必须 raise，不能当解读返回。"""
    import pytest
    from app.core.llm import claude_cli

    class _R:
        returncode = 0
        stdout = 'Failed to authenticate. API Error: 401 {"type":"error"}'
        stderr = ""

    monkeypatch.setattr(claude_cli.subprocess, "run", lambda *a, **k: _R())
    with pytest.raises(RuntimeError):
        claude_cli.generate_text("hi")


def test_router_dispatches_to_claude_cli_with_model(monkeypatch):
    monkeypatch.setattr(llm, "_settings", lambda *a, **k: _CLAUDE)
    seen = {}

    def fake(prompt, model="sonnet", timeout=180, pdf_path=None):
        seen["model"] = model
        return "CLAUDE结果"

    monkeypatch.setattr(llm.claude_cli, "generate_text", fake)
    assert llm.generate_text("hi") == "CLAUDE结果"
    assert seen["model"] == "opus"                            # 用 DB 里配的 claude_model


def test_router_text_falls_back_to_stub_on_error(monkeypatch):
    monkeypatch.setattr(llm, "_settings", lambda *a, **k: _OPENAI)

    def boom(*a, **k):
        raise RuntimeError("网络炸了")

    monkeypatch.setattr(llm.openai_compat, "generate_text", boom)
    out = llm.generate_text("hi", task="brand_digest")
    assert "[stub:brand_digest]" in out                       # 端到端不崩，退 stub


def test_router_image_falls_back_to_stub_on_error(monkeypatch):
    monkeypatch.setattr(llm, "_settings", lambda *a, **k: _CODEX)

    def boom(*a, **k):
        raise RuntimeError("codex 未授权")

    monkeypatch.setattr(llm.codex_image, "generate_image", boom)
    out = llm.generate_image("敦煌飞天")
    assert out.endswith(".png") and "placeholder" in out


def test_router_dispatches_minimax_image(monkeypatch):
    monkeypatch.setattr(llm, "_settings", lambda *a, **k: _MINIMAX_IMAGE)
    seen = {}

    def fake(prompt, base_url, api_key, model="image-01", timeout=60):
        seen.update(base_url=base_url, api_key=api_key, model=model)
        return "https://img.example/out.jpg"

    monkeypatch.setattr(llm.minimax_image, "generate_image", fake)
    assert llm.generate_image("敦煌飞天") == "https://img.example/out.jpg"
    assert seen == {"base_url": "https://img/v1", "api_key": "img-sk", "model": "image-01"}


def test_router_minimax_image_falls_back_to_stub_on_error(monkeypatch):
    monkeypatch.setattr(llm, "_settings", lambda *a, **k: _MINIMAX_IMAGE)

    def boom(*a, **k):
        raise RuntimeError("MiniMax 图片接口失败")

    monkeypatch.setattr(llm.minimax_image, "generate_image", boom)
    out = llm.generate_image("敦煌飞天")
    assert out.endswith(".png") and "placeholder" in out


def test_router_unconfigured_uses_stub(monkeypatch):
    monkeypatch.setattr(llm, "_settings", lambda *a, **k: {**_OPENAI, "text_provider": "stub"})
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
        st.image_api_key = "img-live"
        s.add(st)
        s.commit()
    got = llm._settings()                                     # 路由动态读 db.engine
    assert got["text_provider"] == "openai"
    assert got["openai_api_key"] == "sk-live"
    assert got["openai_model"] == "gpt-x"
    assert got["image_api_key"] == "img-live"


# ── 配置页：权限 ──

def test_page_owner_ok(owner_client):
    r = owner_client.get("/settings/llm")
    assert r.status_code == 200
    assert "模型配置" in r.text
    # 三种模式齐全
    assert "claude-cli" in r.text and "codex" in r.text
    assert "minimax-m3" in r.text
    assert "https://api.minimax.chat/v1" in r.text and "MiniMax-M3" in r.text
    assert "image-01" in r.text


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
            "openai_model": "m", "image_base_url": "https://img/v1",
            "image_api_key": "img-secret", "image_model": "image-01",
            "claude_model": "sonnet"}
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


def test_text_and_image_keys_are_saved_independently(owner_client):
    r = _post(
        owner_client,
        openai_api_key="sk-text-1111",
        image_provider="minimax-m3",
        image_api_key="sk-image-2222",
    )
    assert r.status_code == 303
    got = llm._settings()
    assert got["openai_api_key"] == "sk-text-1111"
    assert got["image_api_key"] == "sk-image-2222"
    assert got["openai_base_url"] == "https://x/v1"
    assert got["image_base_url"] == "https://api.minimax.chat/v1"


def test_empty_image_key_keeps_existing(owner_client):
    _post(owner_client, image_provider="minimax-m3", image_api_key="sk-image-9999")
    _post(owner_client, image_provider="minimax-m3", image_api_key="")
    assert llm._settings()["image_api_key"] == "sk-image-9999"


def test_minimax_m3_option_defaults_model(owner_client):
    r = _post(
        owner_client,
        text_provider="minimax-m3",
        openai_base_url="https://wrong.example/v1",
        openai_model="wrong-model",
    )
    assert r.status_code == 303
    got = llm._settings()
    assert got["text_provider"] == "minimax-m3"
    assert got["openai_base_url"] == "https://api.minimax.chat/v1"
    assert got["openai_model"] == "MiniMax-M3"
    assert got["image_base_url"] == "https://img/v1"


def test_minimax_m3_image_option_defaults_model(owner_client):
    r = _post(
        owner_client,
        text_provider="stub",
        image_provider="minimax-m3",
        openai_base_url="https://wrong.example/v1",
        openai_model="wrong-model",
    )
    assert r.status_code == 303
    got = llm._settings()
    assert got["image_provider"] == "minimax-m3"
    assert got["openai_base_url"] == "https://wrong.example/v1"
    assert got["openai_model"] == "wrong-model"
    assert got["image_base_url"] == "https://api.minimax.chat/v1"
    assert got["image_model"] == "image-01"


# ── 按模块配置模型（预留接口）：resolve 继承/覆盖 + generate_text 按 module 路由 ──

def test_resolve_unconfigured_module_inherits_default(fresh_db):
    """未配置的模块 → 继承 default（知识库锚点）。"""
    from sqlmodel import Session
    from app.core.settings import get_llm_settings, resolve_llm_settings
    with Session(fresh_db) as s:
        d = get_llm_settings(s)                       # default 行
        d.text_provider = "claude-cli"; d.claude_model = "opus"
        s.add(d); s.commit()
        r = resolve_llm_settings(s, "topic")          # 选题库还没配
        assert r["text_provider"] == "claude-cli" and r["claude_model"] == "opus"


def test_resolve_module_override(fresh_db):
    """模块存了自己的 scope 行 → 覆盖 default。"""
    from sqlmodel import Session
    from app.core.settings import LLMSetting, get_llm_settings, resolve_llm_settings
    with Session(fresh_db) as s:
        d = get_llm_settings(s); d.text_provider = "stub"; s.add(d); s.commit()
        s.add(LLMSetting(scope="topic", text_provider="openai", openai_model="gpt-x"))
        s.commit()
        r = resolve_llm_settings(s, "topic")
        assert r["text_provider"] == "openai" and r["openai_model"] == "gpt-x"


def test_resolve_writing_text_inherits_image_own(fresh_db):
    """写作引擎：文本 inherit 继承知识库，图像用自己那套（文本/图像各自判断）。"""
    from sqlmodel import Session
    from app.core.settings import LLMSetting, get_llm_settings, resolve_llm_settings
    with Session(fresh_db) as s:
        d = get_llm_settings(s)
        d.text_provider = "claude-cli"; d.image_provider = "stub"; s.add(d); s.commit()
        s.add(LLMSetting(scope="writing", text_provider="inherit",
                         image_provider="minimax-m3", image_model="image-01",
                         image_base_url="https://img/v1", image_api_key="k"))
        s.commit()
        r = resolve_llm_settings(s, "writing")
        assert r["text_provider"] == "claude-cli"     # 文本继承 default
        assert r["image_provider"] == "minimax-m3"    # 图像用自己
        assert r["image_api_key"] == "k"


def test_generate_text_routes_by_module(fresh_db, monkeypatch):
    """generate_text(module=...) 路由到该模块 provider；未配模块回退 default(=stub)。"""
    from sqlmodel import Session
    from app.core.settings import LLMSetting, get_llm_settings
    with Session(fresh_db) as s:
        d = get_llm_settings(s); d.text_provider = "stub"; s.add(d); s.commit()
        s.add(LLMSetting(scope="topic", text_provider="openai",
                         openai_base_url="https://t/v1", openai_api_key="tk", openai_model="tm"))
        s.commit()
    monkeypatch.setattr(llm.openai_compat, "generate_text", lambda *a, **k: "TOPIC")
    assert llm.generate_text("hi", module="topic") == "TOPIC"        # 路由到 topic 的 openai
    assert "[stub" in llm.generate_text("hi", module="knowledge")    # 知识库未单配 → default=stub
