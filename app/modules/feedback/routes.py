"""⑤数据反馈——占位骨架。菜单已连通，功能待填（负责人：Pumbaa，MET-10）。照 knowledge 样板往这里填。"""
from fastapi import APIRouter
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

_MOD = {"mod_num": "⑤", "mod_name": "数据反馈", "mod_dir": "feedback", "owner": "Pumbaa"}


@router.get("/feedback")
def feedback_home(request: Request):
    return templates.TemplateResponse(request, "module_placeholder.html", _MOD)
