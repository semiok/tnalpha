"""模型配置页（定义者 only）——选 Claude 授权 / Codex 授权 / 其他 API，改完即时生效。"""
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session
from starlette.requests import Request

from app.core import auth, config, runtime
from app.core.db import get_session
from app.core.settings import get_llm_settings

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

_MASK = "••••••"  # api_key 打码占位；POST 收到此值/空 → 不覆盖原 key
MINIMAX_M3_BASE_URL = "https://api.minimax.chat/v1"
MINIMAX_M3_MODEL = "MiniMax-M3"
MINIMAX_IMAGE_MODEL = "image-01"


def _masked(key: str) -> str:
    return f"{_MASK}{key[-4:]}" if key else ""


@router.post("/settings/knowledge-writable")
def toggle_knowledge_writable(request: Request):
    """右上角「开发/演示」按钮：切换全站模式并持久到 DB，切完回首页看新模式。"""
    auth.require_level(request, 2)
    runtime.set_knowledge_writable(not runtime.knowledge_writable())
    return RedirectResponse("/", status_code=303)


@router.get("/settings/llm")
def llm_settings_page(request: Request, session: Session = Depends(get_session)):
    auth.require_model_config(request)
    st = get_llm_settings(session)
    ctx = {
        "st": st,
        "masked_text_key": _masked(st.openai_api_key),
        "masked_image_key": _masked(st.image_api_key),
        "masked_gemini_key": _masked(st.gemini_api_key),
        "masked_perplexity_key": _masked(st.perplexity_api_key),
        "claude_ready": shutil.which("claude") is not None,
        "codex_ready": Path(config.CODEX_AUTH_PATH).expanduser().exists(),
        "minimax_m3_base_url": MINIMAX_M3_BASE_URL,
        "minimax_m3_model": MINIMAX_M3_MODEL,
        "minimax_image_model": MINIMAX_IMAGE_MODEL,
        "saved": request.query_params.get("saved") == "1",
    }
    return templates.TemplateResponse(request, "settings/llm.html", ctx)


@router.post("/settings/llm")
def save_llm_settings(request: Request,
                      text_provider: str = Form("stub"), image_provider: str = Form("stub"),
                      openai_base_url: str = Form(""), openai_api_key: str = Form(""),
                      openai_model: str = Form(""), image_base_url: str = Form(""),
                      image_api_key: str = Form(""), image_model: str = Form(""),
                      claude_model: str = Form("sonnet"), codex_model: str = Form("gpt-5.5"),
                      gemini_api_key: str = Form(""), perplexity_api_key: str = Form(""),
                      session: Session = Depends(get_session)):
    auth.require_model_config(request)
    st = get_llm_settings(session)
    st.text_provider = text_provider
    st.image_provider = image_provider
    if text_provider == "minimax-m3":
        st.openai_base_url = MINIMAX_M3_BASE_URL
        st.openai_model = MINIMAX_M3_MODEL
    else:
        st.openai_base_url = openai_base_url or st.openai_base_url
        st.openai_model = openai_model or st.openai_model
    if image_provider == "minimax-m3":
        st.image_base_url = MINIMAX_M3_BASE_URL
        st.image_model = MINIMAX_IMAGE_MODEL
    else:
        st.image_base_url = image_base_url or st.image_base_url
        st.image_model = image_model or st.image_model
    st.claude_model = claude_model or st.claude_model
    st.codex_model = codex_model or st.codex_model
    # 空 / 打码占位 → 保持原 key 不覆盖（避免打码回填清空）
    if openai_api_key and not openai_api_key.startswith(_MASK):
        st.openai_api_key = openai_api_key
    if image_api_key and not image_api_key.startswith(_MASK):
        st.image_api_key = image_api_key
    if gemini_api_key and not gemini_api_key.startswith(_MASK):
        st.gemini_api_key = gemini_api_key
    if perplexity_api_key and not perplexity_api_key.startswith(_MASK):
        st.perplexity_api_key = perplexity_api_key
    session.add(st)
    session.commit()
    return RedirectResponse("/settings/llm?saved=1", status_code=303)
