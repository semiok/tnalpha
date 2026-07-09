"""角色鉴权与模块权限矩阵。

cookie = hmac(SECRET_KEY, "tnalpha-role:<role>")，无密钥不可伪造，constant-time 比对。
"""
import hashlib
import hmac

from fastapi import HTTPException
from starlette.requests import Request

from app.core import config

COOKIE_NAME = "tnalpha_auth"
COOKIE_MAX_AGE = 7 * 24 * 3600
PUBLIC_PATHS = {"/login", "/health"}
ROLE_LEVEL = {"admin0": 3, "owner": 2, "editor": 1, "publisher": 0}
ROLE_LABELS = {
    "admin0": "管理员",
    "owner": "定义者",
    "editor": "选题者",
    "publisher": "发布者",
}
MODULES = {
    "knowledge": {"num": "①", "label": "知识库", "href": "/"},
    "topic": {"num": "②", "label": "选题库", "href": "/topics"},
    "writing": {"num": "③", "label": "写作引擎", "href": "/writing"},
    "schedule": {"num": "④", "label": "排期版", "href": "/schedule"},
    "feedback": {"num": "⑤", "label": "数据反馈", "href": "/feedback"},
    "permissions": {"num": "⑥", "label": "权限", "href": "/permissions"},
    "prompts": {"num": "⑦", "label": "提示词展示", "href": "/prompts"},
}
MODULE_VIEW: dict[str, set[str]] = {
    "admin0": set(MODULES),
    "owner": {"knowledge", "topic", "writing", "schedule", "feedback"},
    "editor": {"knowledge", "topic", "writing", "schedule", "feedback"},
    "publisher": {"knowledge", "topic", "writing", "schedule", "feedback"},
}
MODULE_WRITE: dict[str, set[str]] = {
    "admin0": set(MODULES),
    "owner": {"knowledge", "topic", "writing", "schedule", "feedback"},
    "editor": {"topic", "writing", "schedule", "feedback"},
    "publisher": {"schedule", "feedback"},
}


def token(role: str) -> str:
    return hmac.new(config.SECRET_KEY.encode(), f"tnalpha-role:{role}".encode(),
                    hashlib.sha256).hexdigest()


def current_role(request: Request) -> str | None:
    cookie = request.cookies.get(COOKIE_NAME, "")
    if not cookie:
        return None
    for role in ROLE_LEVEL:
        if hmac.compare_digest(cookie, token(role)):
            return role
    return None


def is_authed(request: Request) -> bool:
    return current_role(request) is not None


def level_of(role: str | None) -> int:
    return ROLE_LEVEL.get(role, -1)


def label_of(role: str | None) -> str:
    return ROLE_LABELS.get(role or "", "")


def module_for_path(path: str) -> str:
    if path.startswith(("/brands", "/campaigns", "/pool")) or path == "/":
        return "knowledge"
    if path.startswith("/topics"):
        return "topic"
    if path.startswith("/writing"):
        return "writing"
    if path.startswith("/schedule"):
        return "schedule"
    if path.startswith("/feedback"):
        return "feedback"
    if path.startswith("/permissions"):
        return "permissions"
    if path.startswith("/prompts"):
        return "prompts"
    return ""


def can_view_module(role: str | None, module: str) -> bool:
    return module in MODULE_VIEW.get(role or "", set())


def can_write_module(role: str | None, module: str) -> bool:
    return module in MODULE_WRITE.get(role or "", set())


def can_view_path(role: str | None, path: str) -> bool:
    module = module_for_path(path)
    return not module or can_view_module(role, module)


def can_model_config(role: str | None) -> bool:
    return role == "admin0"


def visible_nav(role: str | None) -> list[dict[str, str]]:
    return [
        {"key": key, **meta}
        for key, meta in MODULES.items()
        if can_view_module(role, key)
    ]


def level_for_path(role: str | None, path: str) -> int:
    """兼容旧模板的 level 判断：当前页面可写时给足旧阈值，否则降为只读。"""
    module = module_for_path(path)
    if role == "admin0":
        return 3
    if not module:
        return level_of(role)
    if not can_write_module(role, module):
        return 0
    if module == "knowledge":
        return 2
    return 1


def check_credentials(username: str, password: str) -> str | None:
    """返回角色名或 None。遍历全账号、命中即记录（时序恒定，不短路）。"""
    matched = None
    for user, (pw, role) in config.USERS.items():
        if hmac.compare_digest(username, user) and hmac.compare_digest(password, pw):
            matched = role
    return matched


def require_level(request: Request, n: int) -> None:
    """受控写操作守卫：按当前路径所属模块判断写权限，兼容旧调用签名。"""
    role = current_role(request)
    module = module_for_path(request.url.path)
    if module:
        allowed = can_write_module(role, module)
    else:
        allowed = level_of(role) >= n
    if not allowed:
        raise HTTPException(status_code=403, detail="权限不足")


def require_model_config(request: Request) -> None:
    if not can_model_config(current_role(request)):
        raise HTTPException(status_code=403, detail="权限不足")
