"""④排期版——占位骨架。菜单已连通，功能待填（负责人：Pumbaa，MET-9）。照 knowledge 样板往这里填。"""
from fastapi import APIRouter
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

_MOD = {"mod_num": "④", "mod_name": "排期版", "mod_dir": "schedule", "owner": "Pumbaa"}


@router.get("/schedule")
def schedule_home(request: Request):
    return templates.TemplateResponse(request, "module_placeholder.html", _MOD)
