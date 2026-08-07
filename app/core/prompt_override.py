"""提示词覆盖层：用户可在提示词管理页面修改提示词，运行时动态读取。

设计：
- 代码中的 prompt 函数是「默认值」，不动。
- 用户修改后存入 PromptOverride 表，运行时优先使用覆盖版本。
- 恢复默认 = 删掉覆盖记录，自动回退到代码原始逻辑。

缓存：
- 模块级 dict 缓存，app 启动时加载，保存/删除时刷新，避免每次调用都查库。
"""
import threading
from datetime import datetime
from typing import Any

from sqlmodel import Field, Session, SQLModel, select


def _now() -> datetime:
    return datetime.now()


class PromptOverride(SQLModel, table=True):
    """用户覆盖的提示词模板。"""
    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(index=True, unique=True)        # 唯一标识，如 "writing:article_prompt"
    template: str = Field(default="")                 # 用户修改的模板文本
    updated_at: datetime = Field(default_factory=_now)


# ── 模块级缓存 ──
_cache: dict[str, str] = {}
_lock = threading.Lock()
_loaded = False


def load_cache(session: Session) -> None:
    """从数据库加载所有覆盖到内存缓存。app 启动时调用。"""
    global _loaded
    with _lock:
        rows = session.exec(select(PromptOverride)).all()
        _cache.clear()
        for row in rows:
            _cache[row.key] = row.template
        _loaded = True


def refresh_cache(session: Session) -> None:
    """保存/删除覆盖后刷新缓存。"""
    load_cache(session)


def get_override(key: str) -> str | None:
    """返回覆盖模板，没有则 None。"""
    return _cache.get(key)


def is_customized(key: str) -> bool:
    """该提示词是否被用户自定义过。"""
    return key in _cache


def resolve(key: str, default: str, **variables: Any) -> str:
    """解析提示词：有覆盖用覆盖，没有用默认。

    Args:
        key: 提示词唯一标识
        default: 默认模板字符串（用 {variable} 占位符）
        **variables: 模板变量，用于 .format() 替换

    Returns:
        最终的提示词字符串
    """
    override = _cache.get(key)
    template = override if override is not None else default
    try:
        return template.format(**variables)
    except (KeyError, ValueError, IndexError):
        # 覆盖模板有未知变量或格式错误，回退到默认
        if override is not None:
            try:
                return default.format(**variables)
            except (KeyError, ValueError, IndexError):
                return default
        return default


def save_override(session: Session, key: str, template: str) -> None:
    """保存覆盖（新增或更新）。"""
    existing = session.exec(
        select(PromptOverride).where(PromptOverride.key == key)
    ).first()
    if existing:
        existing.template = template
        existing.updated_at = _now()
        session.add(existing)
    else:
        session.add(PromptOverride(key=key, template=template))
    session.commit()
    refresh_cache(session)


def delete_override(session: Session, key: str) -> None:
    """删除覆盖（恢复默认）。"""
    existing = session.exec(
        select(PromptOverride).where(PromptOverride.key == key)
    ).first()
    if existing:
        session.delete(existing)
        session.commit()
        refresh_cache(session)
