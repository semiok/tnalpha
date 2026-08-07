"""提示词覆盖层：用户可在提示词管理页面修改提示词，运行时动态读取。

设计：
- 代码中的 prompt 函数是「默认值」，不动。
- 用户修改后存入 PromptOverride 表，运行时优先使用覆盖版本。
- 恢复默认 = 删掉覆盖记录，自动回退到代码原始逻辑。

缓存：
- 模块级 dict 缓存，app 启动时加载，保存/删除时刷新，避免每次调用都查库。
"""
import re
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


# ── 安全变量替换 ──
# 匹配 {variable} 或 {obj.attr}，不匹配 {"key": ...} 等字面花括号
_VAR_RE = re.compile(r"\{(\w+)(?:\.(\w+))?\}")
_MISSING = object()


def _safe_substitute(template: str, variables: dict[str, Any]) -> str:
    """安全替换：只替换已知变量，未知 {xxx} 原样保留，不影响字面 {}。"""
    def _replacer(match: re.Match) -> str:
        name = match.group(1)
        attr = match.group(2)
        if name not in variables:
            return match.group(0)          # 未知变量，保持原样
        val = variables[name]
        if attr:
            val = getattr(val, attr, _MISSING)
            if val is _MISSING:
                return match.group(0)      # 属性不存在，保持原样
        return str(val)
    return _VAR_RE.sub(_replacer, template)


def resolve(key: str, default: str, **variables: Any) -> str:
    """解析提示词：有覆盖用覆盖，没有用默认。

    优先用 str.format() 替换变量（支持 {name}、{obj.attr}、{name:spec} 等）。
    如果模板含未知变量或格式错误，回退到安全替换：只替换已知变量，
    未知 {xxx} 原样保留，不影响字面花括号。
    """
    override = _cache.get(key)
    template = override if override is not None else default
    try:
        return template.format(**variables)
    except (KeyError, ValueError, IndexError):
        return _safe_substitute(template, variables)


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
