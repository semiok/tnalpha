"""数据库引擎与会话。SQLite(开发) / Postgres(生产) 同一套代码。"""
from sqlalchemy import inspect, text
from sqlmodel import SQLModel, Session, create_engine

from app.core import config

# SQLite 需要 check_same_thread=False 才能在 FastAPI 多线程下用
_connect_args = {"check_same_thread": False} if config.DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(config.DATABASE_URL, echo=False, connect_args=_connect_args)


def init_db() -> None:
    """建表（开发用；生产用 Alembic 迁移）。导入所有模块的 models 后调用。"""
    import app.core.settings  # noqa: F401  注册 LLMSetting 表
    import app.modules.knowledge.models  # noqa: F401  注册表
    SQLModel.metadata.create_all(engine)
    _ensure_llm_setting_columns()


def _ensure_llm_setting_columns() -> None:
    """开发库轻量补列；已有库不会因为新增设置字段而启动失败。"""
    columns = {c["name"] for c in inspect(engine).get_columns("llmsetting")}
    missing = []
    if "image_base_url" not in columns:
        missing.append(("image_base_url", "VARCHAR", config.IMAGE_BASE_URL))
    if "image_api_key" not in columns:
        missing.append(("image_api_key", "VARCHAR", config.IMAGE_API_KEY))
    if "image_model" not in columns:
        missing.append(("image_model", "VARCHAR", config.IMAGE_PROVIDER_MODEL))
    if not missing:
        return

    with engine.begin() as conn:
        for name, sql_type, default in missing:
            conn.execute(text(f"ALTER TABLE llmsetting ADD COLUMN {name} {sql_type} DEFAULT ''"))
        conn.execute(text(
            "UPDATE llmsetting SET image_base_url = COALESCE(NULLIF(image_base_url, ''), openai_base_url, :image_base_url), "
            "image_api_key = COALESCE(NULLIF(image_api_key, ''), openai_api_key, :image_api_key), "
            "image_model = COALESCE(NULLIF(image_model, ''), :image_model)"
        ), {
            "image_base_url": config.IMAGE_BASE_URL,
            "image_api_key": config.IMAGE_API_KEY,
            "image_model": config.IMAGE_PROVIDER_MODEL,
        })


def get_session():
    """FastAPI 依赖：每请求一个会话。"""
    with Session(engine) as session:
        yield session
