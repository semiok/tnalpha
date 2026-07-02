"""测试夹具：内存 SQLite（每测试独立）+ 三角色 client + anon_client。

写法照 tngen M11：用 cookie 注入角色（`auth.token(role)`），不走登录页。
每个测试一套全新库（StaticPool 单连接内存库），互不污染。
"""
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

# 导入 models 以注册到 SQLModel.metadata
import app.modules.knowledge.models  # noqa: F401
from app.core import auth, config
from app.core.db import get_session
from app.main import app


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """全新内存库 + 依赖覆盖；上传目录指向 tmp，避免污染工作区。"""
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "data"))
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    def _override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = _override
    yield engine
    app.dependency_overrides.clear()
    engine.dispose()


def _client(role: str | None) -> TestClient:
    # 不用 `with`：避免触发 lifespan（那会在真实库建表）；依赖已被 fresh_db 覆盖。
    client = TestClient(app)
    if role is not None:
        client.cookies.set(auth.COOKIE_NAME, auth.token(role))
    return client


@pytest.fixture
def owner_client(fresh_db):
    return _client("owner")


@pytest.fixture
def editor_client(fresh_db):
    return _client("editor")


@pytest.fixture
def publisher_client(fresh_db):
    return _client("publisher")


@pytest.fixture
def anon_client(fresh_db):
    return _client(None)
