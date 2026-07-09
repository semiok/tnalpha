"""core 抽象层测试：llm stub / sources stub / storage 落盘。"""
import io
import os
import re
import types
from datetime import datetime

import pytest

from app.core import llm, sources, storage


def test_version_semver_and_exposed():
    from app import __version__
    from app.main import app
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)   # 语义化版本
    assert app.version == __version__                    # FastAPI 元数据同源


def test_llm_generate_text_nonempty_and_deterministic(monkeypatch):
    # 强制使用 stub，避免受本地 DB provider 配置影响
    monkeypatch.setattr(llm, "_settings", lambda scope="default": {
        "text_provider": "stub", "image_provider": "stub",
        "openai_base_url": "", "openai_api_key": "", "openai_model": "",
        "image_base_url": "", "image_api_key": "", "image_model": "",
        "claude_model": "", "codex_model": "",
    })
    out = llm.generate_text("给品牌写个定位摘要", task="brand_digest")
    assert out and isinstance(out, str)
    assert "brand_digest" in out
    # 确定性：同输入同输出
    assert out == llm.generate_text("给品牌写个定位摘要", task="brand_digest")


def test_llm_generate_image_returns_path(monkeypatch):
    monkeypatch.setattr(llm, "_settings", lambda scope="default": {
        "text_provider": "stub", "image_provider": "stub",
        "openai_base_url": "", "openai_api_key": "", "openai_model": "",
        "image_base_url": "", "image_api_key": "", "image_model": "",
        "claude_model": "", "codex_model": "",
    })
    path = llm.generate_image("敦煌飞天")
    assert isinstance(path, str) and path.endswith(".png")


def test_sources_stub_search_returns_list():
    hits = sources.search("stub", "国潮")
    assert isinstance(hits, list) and len(hits) > 0
    assert set(hits[0]) >= {"title", "summary", "url", "source"}


def test_sources_unready_falls_back_to_stub():
    # 占位源未接入 → 默认回退 stub，仍有结果
    hits = sources.search("xhs", "国潮")
    assert isinstance(hits, list) and len(hits) > 0


def test_sources_unready_raises_without_fallback():
    with pytest.raises(NotImplementedError):
        sources.search("xhs", "国潮", fallback=False)


def test_sources_unknown_raises():
    with pytest.raises(ValueError):
        sources.search("nope", "x")


def test_docparse_unsupported_returns_empty(tmp_path):
    from app.core import docparse
    p = tmp_path / "note.txt"
    p.write_text("hi", encoding="utf-8")
    assert docparse.extract_text(str(p)) == ""


def test_docparse_docx_extracts_text(tmp_path):
    from app.core import docparse
    from docx import Document
    d = Document()
    d.add_paragraph("敦煌壁画的矿物颜料工艺")
    fp = tmp_path / "brand.docx"
    d.save(str(fp))
    assert "敦煌壁画的矿物颜料工艺" in docparse.extract_text(str(fp))


def test_storage_save_upload_writes_file(tmp_path, monkeypatch):
    from app.core import config
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    fake = types.SimpleNamespace(filename="note.txt", file=io.BytesIO(b"hello"))
    path = storage.save_upload(fake, subdir="docs")
    assert os.path.exists(path)
    with open(path, "rb") as fh:
        assert fh.read() == b"hello"


def test_cn_time_filter_converts_server_time_to_china():
    from app.core.timezone import format_display_time, to_display_time
    source = datetime(2026, 7, 8, 12, 30)
    assert to_display_time(source) == datetime(2026, 7, 9, 0, 30)
    assert format_display_time(source, "%Y-%m-%d %H:%M") == "2026-07-09 00:30"
