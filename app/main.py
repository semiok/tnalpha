"""tnalpha 应用入口：装配中间件 + 各模块路由。

新增模块：在 app/modules/<name>/routes.py 写 router，然后在下方 include_router。
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from app import __version__
from app.core import auth, auth_routes, runtime, settings_routes
from app.core.db import init_db
from app.modules.feedback import routes as feedback_routes
from app.modules.knowledge import routes as knowledge_routes
from app.modules.permissions import routes as permissions_routes
from app.modules.prompts import routes as prompts_routes
from app.modules.schedule import routes as schedule_routes
from app.modules.topic import routes as topic_routes
from app.modules.writing import routes as writing_routes


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="TN-Alpha", version=__version__, lifespan=lifespan)


@app.middleware("http")
async def require_login(request: Request, call_next):
    """登录门 + 角色注入：每请求求角色塞 state.role/level；非公开路径未登录跳 /login。"""
    role = auth.current_role(request)
    request.state.role = role
    request.state.level = auth.level_for_path(role, request.url.path)
    request.state.role_label = auth.label_of(role)
    request.state.can_model_config = auth.can_model_config(role)
    request.state.visible_nav = auth.visible_nav(role)
    for module in auth.MODULES:
        setattr(request.state, f"can_write_{module}", auth.can_write_module(role, module))
        setattr(request.state, f"can_view_{module}", auth.can_view_module(role, module))
    request.state.knowledge_writable = runtime.knowledge_writable()  # 开发/演示模式（DB 持久）
    request.state.version = __version__
    if request.url.path not in auth.PUBLIC_PATHS and role is None:
        return RedirectResponse("/login", status_code=303)
    if role is not None and not auth.can_view_path(role, request.url.path):
        return Response("权限不足", status_code=403)
    return await call_next(request)


app.include_router(auth_routes.router)
app.include_router(settings_routes.router)
app.include_router(knowledge_routes.router)
# 模块占位骨架（②③④⑤⑥）：菜单已连通，各贡献者往对应 app/modules/<name>/ 填功能
app.include_router(topic_routes.router)
app.include_router(writing_routes.router)
app.include_router(schedule_routes.router)
app.include_router(feedback_routes.router)
app.include_router(permissions_routes.router)
app.include_router(prompts_routes.router)


@app.get("/health")
def health():
    return {"status": "ok"}
