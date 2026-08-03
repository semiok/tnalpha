"""MiniMax 图片生成的本地落盘契约。"""
from pathlib import Path

from app.core import config
from app.core.llm import minimax_image


class _Response:
    def __init__(self, *, payload=None, content=b""):
        self._payload = payload
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_generate_images_downloads_minimax_urls_to_data_dir(tmp_path, monkeypatch):
    clients = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            clients.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, url, **kwargs):
            assert url == "https://minimax.example/image_generation"
            assert kwargs["json"]["n"] == 2
            return _Response(payload={
                "base_resp": {"status_code": 0},
                "data": {"image_urls": [
                    "https://img.example/one.png?token=short-lived",
                    "https://img.example/two.jpg",
                ]},
            })

        def get(self, url):
            return _Response(content=("bytes:" + url).encode())

    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(minimax_image.httpx, "Client", FakeClient)

    paths = minimax_image.generate_images(
        "test prompt", "https://minimax.example", "secret", n=2,
    )

    assert len(paths) == 2
    assert Path(paths[0]).suffix == ".png"
    assert Path(paths[1]).suffix == ".jpg"
    assert all(Path(path).is_file() for path in paths)
    assert all(Path(path).parent == tmp_path / "data" / "images" for path in paths)
    assert Path(paths[0]).read_bytes().startswith(b"bytes:https://img.example/one.png")
    assert all(client.kwargs["trust_env"] is False for client in clients)
