"""定义者可配置的 LLM 设置（按模块分行，scope 区分）。

「模型配置」页写它，llm router 每次读它选 provider——改完立即生效，无需重启/重部署。
api_key 存本机 DB（gitignored，不进公开仓）。

**按模块配置模型（预留接口）**：scope="default" 是默认锚点（当前=知识库这套）。
未配置 / provider 填 "inherit" 的模块 → 自动继承 default（文本、图像各自判断）。
现在只有知识库开发了，其余模块都走 default——接口已留好，开发时加行即接上。

────────────────────────────────────────────────────────────────
【无 Claude 的贡献者怎么用？（如 lindong）】
本机没有 claude CLI 时，别用 claude-cli provider（会回退 stub、出不了真解析）。
到「模型配置」页把 Provider 从 claude-cli 换成 minimax-m3（或 openai，
填你自己的 Base URL / Model / API Key），保存即生效——default 变了，
知识库解析就走你配的模型。你本地的 DB 和维护者的 DB 是分开的，互不影响。

【未来给你的模块接单独的模型（两步，resolver 不用改）】
  1. 调用处传 module=你的模块名：
        llm.generate_text(prompt, module="topic")        # ②选题库
        llm.generate_image(prompt, module="writing")     # ③写作引擎（图像）
  2. 存一行本模块的 scope（或在模型配置页加个该模块的表单）：
        LLMSetting(scope="topic", text_provider="openai", openai_model="...", ...)
     text_provider / image_provider 填 "inherit" 或留空 = 继承 default。
     没存这行 = 整个模块继承 default（=知识库那套）。
  scope 命名：用模块目录名（knowledge/topic/writing/schedule/feedback）。
────────────────────────────────────────────────────────────────
"""
from sqlalchemy.exc import IntegrityError
from sqlmodel import Field, Session, SQLModel, select

from app.core import config

DEFAULT_LLM_SCOPE = "default"
_INHERIT = ("", "inherit")   # 模块 provider 取此值 → 继承 default


class LLMSetting(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    scope: str = Field(default=DEFAULT_LLM_SCOPE, unique=True, index=True)  # default=锚点；其余=模块名
    text_provider: str = "stub"       # stub | openai | minimax-m3 | claude-cli | codex | inherit(模块继承default)
    image_provider: str = "stub"      # stub | codex | minimax-m3 | inherit
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    image_base_url: str = "https://api.minimax.chat/v1"
    image_api_key: str = ""
    image_model: str = "image-01"
    claude_model: str = "sonnet"      # claude-cli 用（sonnet/opus）
    codex_model: str = "gpt-5.5"      # codex 授权文本用（gpt-5.5，思考 high）


def get_llm_settings(session: Session, scope: str = DEFAULT_LLM_SCOPE) -> LLMSetting | None:
    """取某 scope 的设置行。default 无则用 config/env 初值建；其余模块无行返回 None（由 resolve 回退 default）。"""
    s = session.exec(select(LLMSetting).where(LLMSetting.scope == scope)).first()
    if s is None and scope == DEFAULT_LLM_SCOPE:
        s = LLMSetting(
            scope=DEFAULT_LLM_SCOPE,
            text_provider=config.TEXT_PROVIDER, image_provider=config.IMAGE_PROVIDER,
            openai_base_url=config.OPENAI_BASE_URL, openai_api_key=config.OPENAI_API_KEY,
            openai_model=config.OPENAI_MODEL, image_base_url=config.IMAGE_BASE_URL,
            image_api_key=config.IMAGE_API_KEY, image_model=config.IMAGE_PROVIDER_MODEL,
            claude_model=config.CLAUDE_MODEL, codex_model=config.CODEX_TEXT_MODEL)
        session.add(s)
        try:
            session.commit()
        except IntegrityError:            # 并发首建：另一请求已插 default → 回滚回读
            session.rollback()
            s = session.exec(select(LLMSetting).where(LLMSetting.scope == DEFAULT_LLM_SCOPE)).first()
        else:
            session.refresh(s)
    return s


def resolve_llm_settings(session: Session, scope: str = DEFAULT_LLM_SCOPE) -> dict:
    """把某模块的有效配置解析成 dict：模块行覆盖 default；provider=inherit/空 → 继承 default。
    文本与图像各自判断来源——写作引擎可文本继承知识库、图像用自己那套。"""
    d = get_llm_settings(session, DEFAULT_LLM_SCOPE)
    row = None
    if scope != DEFAULT_LLM_SCOPE:
        row = session.exec(select(LLMSetting).where(LLMSetting.scope == scope)).first()
    text_src = d if (row is None or row.text_provider in _INHERIT) else row
    image_src = d if (row is None or row.image_provider in _INHERIT) else row
    return {
        "text_provider": text_src.text_provider, "image_provider": image_src.image_provider,
        "openai_base_url": text_src.openai_base_url, "openai_api_key": text_src.openai_api_key,
        "openai_model": text_src.openai_model, "claude_model": text_src.claude_model,
        "codex_model": text_src.codex_model,
        "image_base_url": image_src.image_base_url, "image_api_key": image_src.image_api_key,
        "image_model": image_src.image_model,
    }


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
