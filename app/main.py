"""tnalpha 应用入口：装配中间件 + 各模块路由。

新增模块：在 app/modules/<name>/routes.py 写 router，然后在下方 include_router。
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.core import auth, auth_routes
from app.core.db import init_db
from app.modules.knowledge import routes as knowledge_routes


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="tnalpha", lifespan=lifespan)


@app.middleware("http")
async def require_login(request: Request, call_next):
    """登录门 + 角色注入：每请求求角色塞 state.role/level；非公开路径未登录跳 /login。"""
    role = auth.current_role(request)
    request.state.role = role
    request.state.level = auth.level_of(role)
    if request.url.path not in auth.PUBLIC_PATHS and role is None:
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


app.include_router(auth_routes.router)
app.include_router(knowledge_routes.router)


@app.get("/health")
def health():
    return {"status": "ok"}
