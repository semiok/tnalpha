"""②选题库——占位骨架。菜单已连通，功能待填（负责人：lindong，MET-7）。照 knowledge 样板往这里填。"""
from fastapi import APIRouter
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

_MOD = {"mod_num": "②", "mod_name": "选题库", "mod_dir": "topic", "owner": "lindong"}


@router.get("/topics")
def topics_home(request: Request):
    return templates.TemplateResponse(request, "module_placeholder.html", _MOD)
