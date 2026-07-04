"""③写作引擎——占位骨架。菜单已连通，功能待填（负责人：lindong，MET-8）。照 knowledge 样板往这里填。"""
from fastapi import APIRouter
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

_MOD = {"mod_num": "③", "mod_name": "写作引擎", "mod_dir": "writing", "owner": "lindong"}


@router.get("/writing")
def writing_home(request: Request):
    return templates.TemplateResponse(request, "module_placeholder.html", _MOD)
