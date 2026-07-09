"""⑥权限——展示当前账号矩阵。"""
from fastapi import APIRouter
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app.core import auth

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/permissions")
def permissions_home(request: Request):
    rows = []
    for role in ("admin0", "owner", "editor", "publisher"):
        rows.append({
            "role": role,
            "label": auth.label_of(role),
            "username": {"admin0": "admin0", "owner": "admin", "editor": "admin1", "publisher": "admin2"}[role],
            "modules": [
                {
                    "key": key,
                    "label": f"{meta['num']}{meta['label']}",
                    "view": auth.can_view_module(role, key),
                    "write": auth.can_write_module(role, key),
                }
                for key, meta in auth.MODULES.items()
            ],
            "model": auth.can_model_config(role),
        })
    return templates.TemplateResponse(request, "permissions/home.html", {"rows": rows, "modules": auth.MODULES})
