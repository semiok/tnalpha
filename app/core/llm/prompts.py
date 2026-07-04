"""知识库 AI 解析提示词——照抄 tngen（app/llm/prompts.py），保持逻辑一致。

品牌定义流程：单篇文档解读 content_analysis / 深度读图视觉 style_analysis
→ 聚合 aggregate_content / aggregate_style → 反推品牌字段 brand_fields_prompt。
"""


def content_analysis(filename: str, text: str) -> str:
    return (
        "你是品牌内容策划。阅读以下文档，输出一份**供后续选题与文章生成直接复用**的中文解读。"
        "要求：① 这是什么文档；② 核心信息/关键事实/可用素材点（具体、列要点）；③ 可用的内容创作角度。"
        "信息密度高、清晰完整、不要客套、不要逐句复述原文；长度以\"够生成用\"为准，不强行压缩。\n\n"
        f"文档名：{filename}\n\n文档内容：\n{text}"
    )


def style_analysis(filename: str) -> str:
    return (
        "你在为 AI 配图提取视觉风格。阅读这个 PDF（含图片页），输出一份**供图像生成参考**的中文视觉风格蒸馏："
        "色彩、构图、质感、艺术风格、整体氛围；具体、可操作、能直接转成图像生成提示。\n\n"
        f"文档名：{filename}"
    )


def aggregate_content(items: list[tuple[str, str]]) -> str:
    body = "\n\n".join(f"【{name}】\n{a}" for name, a in items)
    return (
        "综合以下各文档解读，输出该品牌主题的**综合内容定义**（中文，供选题与文章生成统一参考）："
        "可用主题方向、核心素材、调性要点。清晰完整、信息密度高。\n\n" + body
    )


def aggregate_style(items: list[tuple[str, str]]) -> str:
    body = "\n\n".join(f"【{name}】\n{s}" for name, s in items)
    return (
        "综合以下各文档的视觉风格解读，输出该品牌的**统一视觉风格指南**（中文，供 AI 配图参考）。\n\n" + body
    )


def brand_fields_prompt(brand_name: str, doc_digest: str) -> str:
    return (
        f"以下是「{brand_name}」品牌的内容解读（已从资料蒸馏）。"
        "请**基于这份内容解读分析**，为该品牌提炼两段供内容生成直接复用的定义：\n"
        "1) 主题调性：品牌基调、文风、目标受众、核心母题（约200字）。\n"
        "2) 内容要求：写作规范——字数 / 平台适配 / 史料或事实准确性 / 配图风格 / 固定尾注等（约200字）。\n\n"
        "严格按以下纯文本格式输出，不要 JSON、不要代码块、不要开场白：\n\n"
        "调性：（约200字主题调性）\n要求：（约200字内容要求）\n\n"
        "【内容解读】\n" + (doc_digest or "")
    )
