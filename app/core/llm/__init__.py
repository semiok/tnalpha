"""统一 LLM 入口——模块只调这里，不直接碰外部 API（铁律 §3）。

provider 由 `config.LLM_PROVIDER` 选，默认 `stub`（返回确定性假数据，保证端到端可跑）。
真实 provider（claude 出文 / codex 出图）后续按同一函数签名接入，调用方无需改动。

    from app.core import llm
    text = llm.generate_text("给这个品牌写个定位摘要", task="brand_digest")
    img  = llm.generate_image("敦煌飞天国潮插画")   # 返回占位路径
"""
from app.core import config
from app.core.llm import stub

# provider 名 → 实现模块（暴露 generate_text / generate_image）
_PROVIDERS = {"stub": stub}


def _provider():
    """按 config 选 provider，未知 provider 回退 stub（保证可跑）。"""
    return _PROVIDERS.get(config.LLM_PROVIDER, stub)


def generate_text(prompt: str, task: str = "default") -> str:
    """文本生成。task 用于区分场景（brand_digest / campaign_digest / …）。"""
    return _provider().generate_text(prompt, task=task)


def generate_image(prompt: str) -> str:
    """图像生成，返回可访问的图片路径（stub 返回占位路径）。"""
    return _provider().generate_image(prompt)
