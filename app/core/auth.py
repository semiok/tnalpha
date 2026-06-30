"""三角色鉴权（搬自 tngen M11，已上线验证）。

cookie = hmac(SECRET_KEY, "tnalpha-role:<role>")，无密钥不可伪造，constant-time 比对。
角色严格包含：owner(2) ⊃ editor(1) ⊃ publisher(0)。
"""
import hashlib
import hmac

from fastapi import HTTPException
from starlette.requests import Request

from app.core import config

COOKIE_NAME = "tnalpha_auth"
COOKIE_MAX_AGE = 7 * 24 * 3600
PUBLIC_PATHS = {"/login", "/health"}
ROLE_LEVEL = {"owner": 2, "editor": 1, "publisher": 0}


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


def check_credentials(username: str, password: str) -> str | None:
    """返回角色名或 None。遍历全账号、命中即记录（时序恒定，不短路）。"""
    matched = None
    for user, (pw, role) in config.USERS.items():
        if hmac.compare_digest(username, user) and hmac.compare_digest(password, pw):
            matched = role
    return matched


def require_level(request: Request, n: int) -> None:
    """受控写操作守卫：level < n 抛 403。安全边界，不依赖中间件 state。"""
    if level_of(current_role(request)) < n:
        raise HTTPException(status_code=403, detail="权限不足")
