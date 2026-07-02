"""core 抽象层测试：llm stub / sources stub / storage 落盘。"""
import io
import os
import types

import pytest

from app.core import llm, sources, storage


def test_llm_generate_text_nonempty_and_deterministic():
    out = llm.generate_text("给品牌写个定位摘要", task="brand_digest")
    assert out and isinstance(out, str)
    assert "brand_digest" in out
    # 确定性：同输入同输出
    assert out == llm.generate_text("给品牌写个定位摘要", task="brand_digest")


def test_llm_generate_image_returns_path():
    path = llm.generate_image("敦煌飞天")
    assert isinstance(path, str) and path.endswith(".png")


def test_sources_stub_search_returns_list():
    hits = sources.search("stub", "国潮")
    assert isinstance(hits, list) and len(hits) > 0
    assert set(hits[0]) >= {"title", "summary", "url", "source"}


def test_sources_unready_falls_back_to_stub():
    # 真实源未接入 → 默认回退 stub，仍有结果
    hits = sources.search("hot", "国潮")
    assert isinstance(hits, list) and len(hits) > 0


def test_sources_unready_raises_without_fallback():
    with pytest.raises(NotImplementedError):
        sources.search("hot", "国潮", fallback=False)


def test_sources_unknown_raises():
    with pytest.raises(ValueError):
        sources.search("nope", "x")


def test_storage_save_upload_writes_file(tmp_path, monkeypatch):
    from app.core import config
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    fake = types.SimpleNamespace(filename="note.txt", file=io.BytesIO(b"hello"))
    path = storage.save_upload(fake, subdir="docs")
    assert os.path.exists(path)
    with open(path, "rb") as fh:
        assert fh.read() == b"hello"
    assert path.endswith(".txt")
