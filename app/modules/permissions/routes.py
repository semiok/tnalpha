"""⑥权限——占位骨架。菜单已连通，功能待填（负责人：Pumbaa，MET-11）。核心 RBAC 已在 core/auth.py。"""
from fastapi import APIRouter
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

_MOD = {"mod_num": "⑥", "mod_name": "权限", "mod_dir": "permissions", "owner": "Pumbaa"}


@router.get("/permissions")
def permissions_home(request: Request):
    return templates.TemplateResponse(request, "module_placeholder.html", _MOD)
