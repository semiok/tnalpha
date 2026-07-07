"""数据库引擎与会话。SQLite(开发) / Postgres(生产) 同一套代码。"""
from sqlmodel import SQLModel, Session, create_engine

from app.core import config

# SQLite 需要 check_same_thread=False 才能在 FastAPI 多线程下用
_connect_args = {"check_same_thread": False} if config.DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(config.DATABASE_URL, echo=False, connect_args=_connect_args)


def init_db() -> None:
    """建表（开发用；生产用 Alembic 迁移）。导入所有模块的 models 后调用。"""
    import app.core.settings  # noqa: F401  注册 LLMSetting 表
    import app.modules.knowledge.models  # noqa: F401  注册表
    import app.modules.topic.models  # noqa: F401  注册 Topic 表
    import app.modules.writing.models  # noqa: F401  注册 Article/Style 表
    import app.modules.schedule.models  # noqa: F401  注册 ScheduleWeek/ScheduleSlot 表
    SQLModel.metadata.create_all(engine)


def get_session():
    """FastAPI 依赖：每请求一个会话。"""
    with Session(engine) as session:
        yield session
