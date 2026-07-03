"""模型配置页（定义者 only）——选 Claude 授权 / Codex 授权 / 其他 API，改完即时生效。"""
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session
from starlette.requests import Request

from app.core import auth, config
from app.core.db import get_session
from app.core.settings import get_llm_settings

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

_MASK = "••••••"  # api_key 打码占位；POST 收到此值/空 → 不覆盖原 key


def _masked(key: str) -> str:
    return f"{_MASK}{key[-4:]}" if key else ""


@router.get("/settings/llm")
def llm_settings_page(request: Request, session: Session = Depends(get_session)):
    auth.require_level(request, 2)
    st = get_llm_settings(session)
    ctx = {
        "st": st,
        "masked_key": _masked(st.openai_api_key),
        "claude_ready": shutil.which("claude") is not None,
        "codex_ready": Path(config.CODEX_AUTH_PATH).expanduser().exists(),
    }
    return templates.TemplateResponse(request, "settings/llm.html", ctx)


@router.post("/settings/llm")
def save_llm_settings(request: Request,
                      text_provider: str = Form("stub"), image_provider: str = Form("stub"),
                      openai_base_url: str = Form(""), openai_api_key: str = Form(""),
                      openai_model: str = Form(""), claude_model: str = Form("sonnet"),
                      session: Session = Depends(get_session)):
    auth.require_level(request, 2)
    st = get_llm_settings(session)
    st.text_provider = text_provider
    st.image_provider = image_provider
    st.openai_base_url = openai_base_url or st.openai_base_url
    st.openai_model = openai_model or st.openai_model
    st.claude_model = claude_model or st.claude_model
    # 空 / 打码占位 → 保持原 key 不覆盖（避免打码回填清空）
    if openai_api_key and not openai_api_key.startswith(_MASK):
        st.openai_api_key = openai_api_key
    session.add(st)
    session.commit()
    return RedirectResponse("/settings/llm", status_code=303)
