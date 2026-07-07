"""测试夹具：内存 SQLite（每测试独立）+ 三角色 client + anon_client。

写法照 tngen M11：用 cookie 注入角色（`auth.token(role)`），不走登录页。
每个测试一套全新库（StaticPool 单连接内存库），互不污染。
"""
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

# 导入 models 以注册到 SQLModel.metadata
import app.core.settings  # noqa: F401  注册 LLMSetting
import app.modules.knowledge.models  # noqa: F401
import app.modules.topic.models  # noqa: F401  注册 Topic 表
import app.modules.writing.models  # noqa: F401  注册 Article/Style 表
from app.core import auth, config, runtime
from app.core import db as _dbmod
from app.core.db import get_session
from app.main import app


@pytest.fixture(autouse=True)
def _force_stub_llm(monkeypatch):
    """全测试强制 LLM=stub：config 默认已改 claude-cli，不拦会真调本机 Claude
    （无 fresh_db 的裸 llm 测试走 config 回退路径）。"""
    monkeypatch.setattr(config, "TEXT_PROVIDER", "stub")
    monkeypatch.setattr(config, "IMAGE_PROVIDER", "stub")


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
    # llm 路由 / runtime 每次调用读 db.engine（动态引用）→ 指到测试库，否则读到真实库
    monkeypatch.setattr(_dbmod, "engine", engine)
    # 测试基线=开发模式（动态知识库）；只读测试内自行 set False
    runtime.set_knowledge_writable(True)
    # 测试强制 LLM=stub（本机有 claude CLI，默认 claude-cli 会真调用、破坏确定性）
    from app.core.settings import get_llm_settings
    with Session(engine) as _s:
        _st = get_llm_settings(_s)
        _st.text_provider = "stub"
        _st.image_provider = "stub"
        _s.add(_st)
        _s.commit()

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
