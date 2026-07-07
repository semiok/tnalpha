"""统一 LLM 入口——模块只调这里，不直接碰外部 API（铁律 §3）。

对外签名不变（generate_text/generate_image），调用方无需改动。
provider 由定义者在「模型配置」页设置（存 DB），router 每次读 DB 选：
  文本 stub | openai | minimax-m3 | claude-cli ；图像 stub | codex | minimax-m3 。
任何 provider 失败 / 未配置 → 回退 stub，保证端到端不崩。

    from app.core import llm
    text = llm.generate_text("给品牌写个定位摘要", task="brand_digest")
    img  = llm.generate_image("敦煌飞天国潮插画")   # 返回图片路径
"""
from sqlmodel import Session

from app.core import config, db
from app.core.llm import claude_cli, codex_image, codex_text, minimax_image, openai_compat, stub


def _settings(scope: str = "default") -> dict:
    """读某模块 scope 的有效 DB 设置（模块未配→继承 default）；DB 不可用/无表时回退 config 默认。"""
    try:
        from app.core.settings import resolve_llm_settings
        with Session(db.engine) as s:
            return resolve_llm_settings(s, scope)
    except Exception as e:
        print(f"[llm] 读 DB 设置失败，回退 config 默认：{e}")
        return {
            "text_provider": config.TEXT_PROVIDER, "image_provider": config.IMAGE_PROVIDER,
            "openai_base_url": config.OPENAI_BASE_URL, "openai_api_key": config.OPENAI_API_KEY,
            "openai_model": config.OPENAI_MODEL, "image_base_url": config.IMAGE_BASE_URL,
            "image_api_key": config.IMAGE_API_KEY, "image_model": config.IMAGE_PROVIDER_MODEL,
            "claude_model": config.CLAUDE_MODEL, "codex_model": config.CODEX_TEXT_MODEL,
        }


def generate_text(prompt: str, task: str = "default", pdf_path: str | None = None,
                  module: str = "default", attachments: list[str] | None = None,
                  fallback: bool = True) -> str:
    """module=模块名，按模块选模型（未配→继承 default=知识库锚点）。task 仅供 stub 标注。
    pdf_path/attachments 非空=深度读图（PDF 图片页 / 图片；只 claude-cli / codex 真读，其余 provider 忽略、仅按文本）。"""
    st = _settings(module)
    p = st["text_provider"]
    try:
        if p in ("openai", "minimax-m3"):
            return openai_compat.generate_text(
                prompt, st["openai_base_url"], st["openai_api_key"],
                st["openai_model"], timeout=config.LLM_TIMEOUT)
        if p == "claude-cli":
            return claude_cli.generate_text(prompt, st["claude_model"], timeout=config.LLM_TIMEOUT,
                                            pdf_path=pdf_path, attachments=attachments)
        if p == "codex":       # Codex 授权文本（gpt-5.5）；深度读图：PDF→input_file、图片→input_image
            return codex_text.generate_text(prompt, st["codex_model"], timeout=config.LLM_TIMEOUT,
                                            pdf_path=pdf_path, attachments=attachments)
    except Exception as e:
        if not fallback:
            raise RuntimeError(f"文本 provider '{p}' 调用失败：{e}") from e
        print(f"[llm] 文本 provider '{p}' 失败，回退 stub：{e}")
    return stub.generate_text(prompt, task=task)


def generate_image(prompt: str, module: str = "default", fallback: bool = True) -> str:
    """module=模块名，按模块选图像模型（未配→继承 default）。"""
    st = _settings(module)
    p = st["image_provider"]
    try:
        if p == "codex":
            return codex_image.generate_image(prompt)
        if p == "minimax-m3":
            return minimax_image.generate_image(
                prompt, st["image_base_url"], st["image_api_key"],
                st["image_model"], timeout=config.IMAGE_TIMEOUT)
    except Exception as e:
        if not fallback:
            raise RuntimeError(f"图像 provider '{p}' 调用失败：{e}") from e
        print(f"[llm] 图像 provider '{p}' 失败，回退 stub：{e}")
    return stub.generate_image(prompt)


def generate_images(prompt: str, module: str = "default", n: int = 4,
                    fallback: bool = True) -> list[str]:
    """批量生成 n 张图，返回 URL 列表。

    minimax 用 API 的 n 参数一次出图（高效）；codex/stub 退化为调用 n 次单图。
    fallback=True 时整批失败回退 n 张 stub 图；fallback=False 抛 RuntimeError。
    """
    st = _settings(module)
    p = st["image_provider"]
    try:
        if p == "codex":
            return [codex_image.generate_image(prompt) for _ in range(n)]
        if p == "minimax-m3":
            return minimax_image.generate_images(
                prompt, st["image_base_url"], st["image_api_key"],
                st["image_model"], n=n, timeout=config.IMAGE_TIMEOUT)
    except Exception as e:
        if not fallback:
            raise RuntimeError(f"图像 provider '{p}' 调用失败：{e}") from e
        print(f"[llm] 图像 provider '{p}' 失败，回退 stub：{e}")
    return [stub.generate_image(prompt) for _ in range(n)]
