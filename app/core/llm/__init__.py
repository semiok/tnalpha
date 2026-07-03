"""统一 LLM 入口——模块只调这里，不直接碰外部 API（铁律 §3）。

对外签名不变（generate_text/generate_image），调用方无需改动。
provider 由定义者在「模型配置」页设置（存 DB），router 每次读 DB 选：
  文本 stub | openai | claude-cli ；图像 stub | codex 。
任何 provider 失败 / 未配置 → 回退 stub，保证端到端不崩。

    from app.core import llm
    text = llm.generate_text("给品牌写个定位摘要", task="brand_digest")
    img  = llm.generate_image("敦煌飞天国潮插画")   # 返回图片路径
"""
from sqlmodel import Session

from app.core import config, db
from app.core.llm import claude_cli, codex_image, openai_compat, stub


def _settings() -> dict:
    """读 DB 设置；DB 不可用/无表时回退 config 默认（保证测试与首启不崩）。"""
    try:
        from app.core.settings import get_llm_settings
        with Session(db.engine) as s:
            st = get_llm_settings(s)
            return {
                "text_provider": st.text_provider, "image_provider": st.image_provider,
                "openai_base_url": st.openai_base_url, "openai_api_key": st.openai_api_key,
                "openai_model": st.openai_model, "claude_model": st.claude_model,
            }
    except Exception as e:
        print(f"[llm] 读 DB 设置失败，回退 config 默认：{e}")
        return {
            "text_provider": config.TEXT_PROVIDER, "image_provider": config.IMAGE_PROVIDER,
            "openai_base_url": config.OPENAI_BASE_URL, "openai_api_key": config.OPENAI_API_KEY,
            "openai_model": config.OPENAI_MODEL, "claude_model": config.CLAUDE_MODEL,
        }


def generate_text(prompt: str, task: str = "default") -> str:
    st = _settings()
    p = st["text_provider"]
    try:
        if p == "openai":
            return openai_compat.generate_text(
                prompt, st["openai_base_url"], st["openai_api_key"],
                st["openai_model"], timeout=config.LLM_TIMEOUT)
        if p == "claude-cli":
            return claude_cli.generate_text(prompt, st["claude_model"], timeout=config.LLM_TIMEOUT)
    except Exception as e:
        print(f"[llm] 文本 provider '{p}' 失败，回退 stub：{e}")
    return stub.generate_text(prompt, task=task)


def generate_image(prompt: str) -> str:
    st = _settings()
    p = st["image_provider"]
    try:
        if p == "codex":
            return codex_image.generate_image(prompt)
    except Exception as e:
        print(f"[llm] 图像 provider '{p}' 失败，回退 stub：{e}")
    return stub.generate_image(prompt)
