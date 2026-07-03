"""定义者可配置的 LLM 设置（单行 DB 表，id 固定=1）。

「模型配置」页写它，llm router 每次读它选 provider——改完立即生效，无需重启/重部署。
api_key 存本机 DB（gitignored，不进公开仓）。
"""
from sqlalchemy.exc import IntegrityError
from sqlmodel import Field, Session, SQLModel

from app.core import config


class LLMSetting(SQLModel, table=True):
    id: int | None = Field(default=1, primary_key=True)
    text_provider: str = "stub"       # stub | openai | claude-cli
    image_provider: str = "stub"      # stub | codex
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    claude_model: str = "sonnet"


def get_llm_settings(session: Session) -> LLMSetting:
    """取单行设置；无则用 config/env 初值建默认行。"""
    s = session.get(LLMSetting, 1)
    if s is None:
        s = LLMSetting(
            id=1, text_provider=config.TEXT_PROVIDER, image_provider=config.IMAGE_PROVIDER,
            openai_base_url=config.OPENAI_BASE_URL, openai_api_key=config.OPENAI_API_KEY,
            openai_model=config.OPENAI_MODEL, claude_model=config.CLAUDE_MODEL)
        session.add(s)
        try:
            session.commit()
        except IntegrityError:            # 并发首建：另一请求已插 id=1 → 回滚回读
            session.rollback()
            s = session.get(LLMSetting, 1)
        else:
            session.refresh(s)
    return s


class AppSetting(SQLModel, table=True):
    """全站运行时设置（单行，id 固定=1）。定义者在页面切换、持久保存。"""
    id: int | None = Field(default=1, primary_key=True)
    knowledge_writable: bool = True   # True=开发模式(动态知识库) / False=演示模式(只读演示壳)


def get_app_settings(session: Session) -> AppSetting:
    """取单行 app 设置；无则用 config/env 初值建默认行。"""
    s = session.get(AppSetting, 1)
    if s is None:
        s = AppSetting(id=1, knowledge_writable=config.KNOWLEDGE_WRITABLE)
        session.add(s)
        try:
            session.commit()
        except IntegrityError:            # 并发首建：回滚回读
            session.rollback()
            s = session.get(AppSetting, 1)
        else:
            session.refresh(s)
    return s
