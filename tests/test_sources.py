"""skill 式搜索源：catalog 自描述 / 各 adapter parse / enabled 门控 / gather 容错。

真实源全自包含（urllib/bs4）；测试 monkeypatch HTTP 层与抓取，不触网、确定性。
"""
import pytest

from app.core import config, sources
from app.core.sources import gemini, sogou, sonar


# ---------- catalog（UI 自描述）----------

def test_catalog_hides_stub_and_lists_sources():
    cat = sources.catalog()
    names = [c["name"] for c in cat]
    assert "stub" not in names                      # stub 不进 UI
    assert names == ["google", "mp", "sonar", "xhs"]  # 顺序=勾选框顺序
    for c in cat:
        assert set(c) >= {"name", "label", "emoji", "paid", "default_on", "enabled"}


def test_catalog_metadata_flags():
    cat = {c["name"]: c for c in sources.catalog()}
    assert cat["google"]["default_on"] is True and cat["google"]["paid"] is False
    assert cat["sonar"]["paid"] is True and cat["sonar"]["default_on"] is False
    assert cat["xhs"]["enabled"] is False           # 占位灰掉


# ---------- google（gemini grounding）----------

def test_google_enabled_follows_key(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    assert gemini.GoogleAdapter().is_available() is False
    monkeypatch.setattr(config, "GEMINI_API_KEY", "k")
    assert gemini.GoogleAdapter().is_available() is True


def test_google_search_parses_answer_and_citations(monkeypatch):
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


def test_google_search_without_key_raises(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    with pytest.raises(NotImplementedError):
        gemini.GoogleAdapter().search("x")


# ---------- sonar（perplexity）----------

def test_sonar_paid_and_parse(monkeypatch):
    a = sonar.SonarAdapter()
    assert a.paid is True
    monkeypatch.setattr(config, "PERPLEXITY_API_KEY", "k")
    fake = {"choices": [{"message": {"content": "实时热点内容"}}],
            "citations": ["https://a", "https://b"]}
    monkeypatch.setattr(sonar._http, "post_json", lambda *a, **k: fake)
    hits = a.search("敦煌")
    assert hits[0]["summary"] == "实时热点内容" and hits[0]["source"] == "sonar"
    assert hits[0]["url"] == "https://a"


def test_sonar_disabled_without_key(monkeypatch):
    monkeypatch.setattr(config, "PERPLEXITY_API_KEY", "")
    assert sonar.SonarAdapter().is_available() is False


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

def test_gather_merges_and_skips_failures(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(config, "PERPLEXITY_API_KEY", "")   # sonar 不可用 → 跳过
    monkeypatch.setattr(gemini._http, "post_json", lambda *a, **k: {
        "candidates": [{"content": {"parts": [{"text": "答案"}]}, "groundingMetadata": {}}]})
    # 搜狗抓取抛错 → gather 应吞掉
    monkeypatch.setattr(sogou, "_fetch", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("反爬")))
    hits = sources.gather(["google", "mp", "sonar", "xhs"], "敦煌", per_source=3)
    assert any(h["source"] == "google" for h in hits)       # google 命中
    assert all(h["source"] != "sonar" for h in hits)        # sonar 无 key 被跳过


def test_gather_empty_query_returns_empty():
    assert sources.gather(["google"], "  ") == []
    assert sources.gather([], "敦煌") == []
