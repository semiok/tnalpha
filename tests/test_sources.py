"""skill 式搜索源：catalog 自描述 / 各 adapter parse / enabled 门控 / gather 容错。

真实源全自包含（urllib/bs4）；测试 monkeypatch HTTP 层与抓取，不触网、确定性。
"""
import pytest

from app.core import config, sources
from app.core.sources import gemini, sogou


# ---------- catalog（UI 自描述）----------

def test_catalog_hides_stub_and_lists_sources(fresh_db):
    cat = sources.catalog()
    names = [c["name"] for c in cat]
    assert "stub" not in names                      # stub 不进 UI
    assert names == ["google", "mp", "xhs"]           # 顺序=勾选框顺序
    for c in cat:
        assert set(c) >= {"name", "label", "emoji", "paid", "default_on", "enabled"}


def test_catalog_metadata_flags(fresh_db):
    cat = {c["name"]: c for c in sources.catalog()}
    assert cat["google"]["default_on"] is True and cat["google"]["paid"] is False
    assert "sonar" not in cat
    assert cat["mp"]["label"] == "搜狗公众号"
    assert cat["xhs"]["enabled"] is False           # 占位灰掉


# ---------- google（gemini grounding）----------

def test_google_enabled_follows_key(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    assert gemini.GoogleAdapter().is_available() is False
    monkeypatch.setattr(config, "GEMINI_API_KEY", "k")
    assert gemini.GoogleAdapter().is_available() is True


def test_google_search_parses_answer_and_citations(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "k")
    fake = {"candidates": [{
        "content": {"parts": [{"text": "敦煌近期有大展"}]},
        "groundingMetadata": {"groundingChunks": [
            {"web": {"title": "新华网", "uri": "https://x.com/a"}}]},
    }]}
    monkeypatch.setattr(gemini._http, "post_json", lambda *a, **k: fake)
    hits = gemini.GoogleAdapter().search("敦煌")
    assert hits[0]["summary"] == "敦煌近期有大展" and hits[0]["source"] == "google"
    assert hits[0]["url"] == "https://x.com/a"       # 首命中带首条引用链接
    assert hits[1]["title"] == "新华网"               # 引用来源附在后


def test_google_search_without_key_raises(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    with pytest.raises(NotImplementedError):
        gemini.GoogleAdapter().search("x")


# ---------- 搜狗公众号（抓取解析）----------

_SOGOU_HTML = """
<ul class="news-list">
  <li id="sogou_vr_11002601_box_0">
    <div class="txt-box">
      <h3><a target="_blank" href="/link?url=ABC">敦煌文创新品</a></h3>
      <p class="txt-info">九色鹿、飞天、藻井三大系列上新。</p>
      <div><span class="all-time-y2">敦煌智慧旅游官方</span></div>
    </div>
  </li>
  <li id="other_box">忽略</li>
</ul>
"""


def test_sogou_parse_extracts_fields():
    hits = sogou._parse(_SOGOU_HTML)
    assert len(hits) == 1
    h = hits[0]
    assert h["title"] == "敦煌文创新品" and "九色鹿" in h["summary"]
    assert h["source"] == "敦煌智慧旅游官方"
    assert h["url"] == "https://weixin.sogou.com/link?url=ABC"


def test_sogou_available_no_key():
    assert sogou.SogouAdapter().is_available() is True   # 无需 key


# ---------- gather（批量、容错）----------

def test_gather_merges_and_skips_failures(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(gemini._http, "post_json", lambda *a, **k: {
        "candidates": [{"content": {"parts": [{"text": "答案"}]}, "groundingMetadata": {}}]})
    # 搜狗抓取抛错 → gather 应吞掉
    monkeypatch.setattr(sogou, "_fetch", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("反爬")))
    hits = sources.gather(["google", "mp", "sonar", "xhs"], "敦煌", per_source=3)
    assert any(h["source"] == "google" for h in hits)       # google 命中
    assert all(h["source"] != "sonar" for h in hits)        # sonar 未注册，被跳过


def test_gather_empty_query_returns_empty():
    assert sources.gather(["google"], "  ") == []
    assert sources.gather([], "敦煌") == []


# ---------- key 解析：DB(模型配置页) 优先，env 回退 ----------

def test_search_api_key_db_over_env(fresh_db, monkeypatch):
    from sqlmodel import Session

    from app.core import settings
    monkeypatch.setattr(config, "GEMINI_API_KEY", "env-key")
    with Session(fresh_db) as s:
        assert settings.search_api_key(s, "gemini") == "env-key"        # DB 空 → 回退 env
        st = settings.get_llm_settings(s)
        st.gemini_api_key = "db-key"
        s.add(st); s.commit()
        assert settings.search_api_key(s, "gemini") == "db-key"          # DB 有值 → 覆盖 env
