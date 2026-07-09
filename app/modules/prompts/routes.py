"""⑦提示词展示：只读列出各模块按钮会注入的 prompt。"""
from dataclasses import dataclass
from datetime import date, datetime
from types import SimpleNamespace

from fastapi import APIRouter, Depends
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
from starlette.requests import Request

from app.core.db import get_session
from app.core.llm import prompts as knowledge_prompts
from app.modules.feedback.experience import (
    PublishedSample,
    _draft_prompt,
    campaign_experience_context,
    experience_reference_strategy_text,
)
from app.modules.knowledge.analysis import _campaign_digest_prompt
from app.modules.knowledge.models import Brand, Campaign
from app.modules.schedule.models import DEFAULT_RECOMMEND_PROMPT, ScheduleMetric, ScheduleSetting, ScheduleSlot
from app.modules.topic.contract import KnowledgeContext
from app.modules.topic.generate import _manual_prompt, _topics_prompt
from app.modules.topic.models import Topic
from app.modules.writing import routes as writing_prompts
from app.modules.writing.debate import ROLES, _debate_prompt, _review_prompt, rewrite_prompt
from app.modules.writing.models import Article, Style

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@dataclass
class PromptItem:
    module: str
    action: str
    source: str
    prompt: str
    note: str = ""


def _first_brand(session: Session) -> Brand | None:
    return session.exec(select(Brand).order_by(Brand.id)).first()


def _first_campaign(session: Session, brand_id: int | None) -> Campaign | None:
    if brand_id is None:
        return None
    return session.exec(
        select(Campaign).where(Campaign.brand_id == brand_id).order_by(Campaign.is_default.desc(), Campaign.id)
    ).first()


def _sample_context(session: Session, brand: Brand | None, campaign_id: int | None) -> KnowledgeContext:
    if brand is not None and brand.id is not None:
        try:
            return KnowledgeContext.load(session, brand.id, campaign_id)
        except Exception:
            pass
    return KnowledgeContext(
        brand_prompt="品牌调性示例：克制、准确、有文化现场感。",
        content_notes="内容要求示例：事实准确，避免空泛抒情，适配公众号/小红书。",
        doc_digest="品牌资料综合示例：这里会注入知识库 AI 解析后的品牌资料摘要。",
        style_digest="视觉风格示例：低饱和暖色、文物质感、留白构图。",
        campaign_digest="活动简报示例：这里会注入 campaign 的选题方向、时效节点和关键素材。",
        pool_materials=["资料包示例：展品清单、新闻稿、图片说明。"],
        pool_experiences=["经验包示例：具体物件切入优于抽象制度解释，开头要有生活化问题。"],
    )


def _sample_topic(session: Session, brand: Brand | None, campaign: Campaign | None) -> Topic:
    if brand is not None and brand.id is not None:
        topic = session.exec(select(Topic).where(Topic.brand_id == brand.id).order_by(Topic.created_at.desc())).first()
        if topic is not None:
            return topic
    return Topic(
        brand_id=brand.id if brand and brand.id else 1,
        campaign_id=campaign.id if campaign else None,
        title="在边塞练字的人：一枚习字简，如何把丝路写进日常？",
        outline="从一枚习字简切入，把汉代书写、邮驿与普通人的日常练习连起来。",
        angle="用具体物件连接古代制度与现代生活经验。",
        audience="亲子家庭 / 历史与艺术爱好者",
        timeliness="中",
        materials="习字简、邮驿制度、边塞生活",
        image_hint="竹简、手写字迹、边塞色调",
        publish_window="展期中段或周末前",
        status="采纳",
    )


def _sample_campaign(brand: Brand | None, campaign: Campaign | None) -> Campaign:
    if campaign is not None:
        return campaign
    return Campaign(
        brand_id=brand.id if brand and brand.id else 1,
        name="丝路有多长",
        campaign_digest="活动简报示例：展览主题、时效节点、可用选题方向和关键素材。",
    )


def _sample_article(topic: Topic) -> Article:
    return Article(
        topic_id=topic.id or 1,
        campaign_id=topic.campaign_id,
        title=topic.title or "示例文章",
        body=(
            "正文示例：从一枚习字简开始，读者看到的不是抽象历史，而是一个人如何练字、"
            "传递信息，并把遥远的丝路写进日常。"
        ),
        image_prompt="竹简、墨迹、边塞光线",
        image_url="",
        platform="微信公众号",
        word_count=800,
    )


def _sample_style(campaign: Campaign | None) -> Style:
    return Style(
        campaign_id=campaign.id if campaign and campaign.id else 1,
        name="展览叙事风",
        summary="以具体物件开场，用短段落推进现场感；语言准确但不过度学术，结尾落到观众可感的生活问题。",
        source="preset",
        is_default=True,
    )


def _feedback_sample(topic: Topic, article: Article, brand: Brand | None) -> PublishedSample:
    slot = ScheduleSlot(
        week_id=1,
        article_id=article.id or 1,
        topic_id=topic.id or 1,
        brand_id=brand.id if brand and brand.id else topic.brand_id,
        campaign_id=topic.campaign_id,
        publish_date=date(2026, 7, 8),
        publish_time="09:30",
        platform="微信公众号",
        status="已发布",
        published_url="https://example.com/article",
        published_at=datetime(2026, 7, 8, 9, 30),
    )
    metric = ScheduleMetric(
        slot_id=1,
        article_id=article.id or 1,
        topic_id=topic.id or 1,
        brand_id=slot.brand_id,
        campaign_id=slot.campaign_id,
        wechat_read=1200,
        wechat_like=88,
        wechat_share=24,
        notes="评论集中在具体物件和现代类比是否清楚。",
    )
    return PublishedSample(
        slot=slot,
        metric=metric,
        article=article,
        topic=topic,
        campaign_name="品牌常青" if topic.campaign_id is None else "丝路有多长",
        performance_level="高表现",
        score=1880,
    )


def _schedule_prompt(session: Session) -> str:
    setting = session.get(ScheduleSetting, 1)
    return setting.recommend_prompt if setting and setting.recommend_prompt else DEFAULT_RECOMMEND_PROMPT


def _prompt_items(session: Session, mode: str = "template") -> list[PromptItem]:
    template = mode == "template"
    if template:
        brand = Brand(
            id=1,
            name="{brand_name}",
            brand_prompt="{brand_prompt}",
            content_notes="{content_notes}",
            doc_digest="{doc_digest}",
            style_digest="{style_digest}",
        )
        campaign = Campaign(
            id=1,
            brand_id=1,
            name="{campaign_name}",
            campaign_digest="{campaign_digest}",
        )
        topic = Topic(
            id=1,
            brand_id=1,
            campaign_id=1,
            title="{topic.title}",
            outline="{topic.outline}",
            angle="{topic.angle}",
            audience="{topic.audience}",
            timeliness="{topic.timeliness}",
            materials="{topic.materials}",
            image_hint="{topic.image_hint}",
            publish_window="{topic.publish_window}",
            status="采纳",
        )
        sample_campaign = campaign
        ctx = KnowledgeContext(
            brand_prompt="{brand_prompt}",
            content_notes="{content_notes}",
            doc_digest="{doc_digest}",
            style_digest="{style_digest}",
            campaign_digest="{campaign_digest}",
            pool_materials=["{pool_materials}"],
            pool_experiences=["{pool_experiences}"],
        )
    else:
        brand = _first_brand(session)
        topic = _sample_topic(session, brand, None)
        campaign = _first_campaign(session, brand.id if brand and brand.id else None)
        if topic.campaign_id:
            campaign = session.get(Campaign, topic.campaign_id) or campaign
        sample_campaign = _sample_campaign(brand, campaign)
        ctx = _sample_context(session, brand, topic.campaign_id or (sample_campaign.id if campaign else None))
    style = _sample_style(sample_campaign)
    article = _sample_article(topic)
    sample = _feedback_sample(topic, article, brand)
    role_key, role_name, role_stance = ROLES[0]

    def value(template_value: str, preview_value: str) -> str:
        return template_value if template else preview_value

    unified_experience = value(
        "{campaign_overall_experience_pack}",
        campaign_experience_context(
            session,
            topic.brand_id,
            topic.campaign_id,
            platform=article.platform,
            task="writing",
            inherited_packs=ctx.pool_experiences,
        ),
    )

    return [
        PromptItem(
            "通用经验策略",
            "Campaign 总体经验包引用策略",
            "app/modules/feedback/experience.py::experience_reference_strategy_text",
            experience_reference_strategy_text(
                current_count=0 if template else 3,
                inherited_count=1 if (template or ctx.pool_experiences) else 0,
            ),
            "②选题库和③写作引擎共用；当前经验不足时自动提高继承经验权重。",
        ),
        PromptItem(
            "①知识库",
            "AI 解析 / 单篇资料解读",
            "app/core/llm/prompts.py::content_analysis",
            knowledge_prompts.content_analysis(
                value("{filename}", "示例资料.pdf"),
                value("{extracted_text}", "这里会注入资料抽取出的正文，最长约 12000 字。"),
            ),
            "品牌资料上传后后台解析时使用。",
        ),
        PromptItem(
            "①知识库",
            "AI 解析 / 深度读图视觉风格",
            "app/core/llm/prompts.py::style_analysis",
            knowledge_prompts.style_analysis(value("{filename}", "示例资料.pdf")),
            "勾选深度读图的 PDF 或图片资料会用 vision 附件读取。",
        ),
        PromptItem(
            "①知识库",
            "AI 解析 / 聚合品牌内容定义",
            "app/core/llm/prompts.py::aggregate_content",
            knowledge_prompts.aggregate_content([
                (value("{filename}", "示例资料.pdf"), value("{doc_ai_analysis}", "单篇 AI 解读结果会放在这里。"))
            ]),
        ),
        PromptItem(
            "①知识库",
            "AI 解析 / 聚合视觉风格",
            "app/core/llm/prompts.py::aggregate_style",
            knowledge_prompts.aggregate_style([
                (value("{filename}", "示例资料.pdf"), value("{style_summary}", "单篇视觉风格解读会放在这里。"))
            ]),
        ),
        PromptItem(
            "①知识库",
            "AI 解析 / 反推品牌调性和内容要求",
            "app/core/llm/prompts.py::brand_fields_prompt",
            knowledge_prompts.brand_fields_prompt(brand.name if brand else "示例品牌", ctx.doc_digest),
        ),
        PromptItem(
            "①知识库",
            "Campaign AI 解析 / 活动选题简报",
            "app/modules/knowledge/analysis.py::_campaign_digest_prompt",
            _campaign_digest_prompt(
                "【品牌定义】\n"
                f"{ctx.brand_prompt}\n\n【活动资料】\n"
                + value("{campaign_docs_and_pool_refs}", "这里会注入 campaign 上传资料、引用资料包和引用经验包。")
            ),
            "新建或更新 campaign 资料后后台解析时使用。",
        ),
        PromptItem(
            "②选题库",
            "生成候选选题",
            "app/modules/topic/generate.py::_topics_prompt",
            _topics_prompt(
                ctx,
                existing_titles=[value("{existing_topic_titles}", "已有选题示例：古人如何写信？")],
                count=5,
                hot_hits=[{
                    "title": value("{hot_hit.title}", "搜狗公众号命中示例"),
                    "summary": value("{hot_hit.summary}", "这里会注入 weixin.sogou.com 搜到的微信内容摘要。"),
                    "source": "搜狗公众号",
                }],
                campaign_experience=value(
                    "{campaign_overall_experience_pack}",
                    campaign_experience_context(
                        session,
                        topic.brand_id,
                        topic.campaign_id,
                        task="topic",
                        inherited_packs=ctx.pool_experiences,
                        rejection_topics=[
                            Topic(
                                brand_id=topic.brand_id,
                                campaign_id=topic.campaign_id,
                                title="过于抽象的丝路制度科普",
                                rejection_reason="缺少具体物件和观众视角，容易写成说明牌。",
                                status="回收站",
                            )
                        ],
                    ),
                ),
            ),
            "默认读取 Campaign 总体经验包；选题侧重点会优先使用切口、标题、回收站不采纳原因和发布表现。",
        ),
        PromptItem(
            "②选题库",
            "手动上传选题 / 补全字段",
            "app/modules/topic/generate.py::_manual_prompt",
            _manual_prompt(ctx, [
                value("{manual_title_1}", "用户手动输入的标题一"),
                value("{manual_title_2}", "用户手动输入的标题二"),
            ], value("{campaign_overall_experience_pack}", unified_experience)),
            "标题必须逐字保留，模型只补全纲要、受众、素材等字段。",
        ),
        PromptItem(
            "③写作引擎",
            "生成图文",
            "app/modules/writing/routes.py::_article_prompt",
            writing_prompts._article_prompt(
                topic,
                ctx,
                style,
                platform=article.platform,
                word_count=article.word_count,
                writing_experience=unified_experience,
            ),
            "默认注入 Campaign 总体经验包；写作侧重点会优先使用审核退回、标题结构、事实和平台语气经验。",
        ),
        PromptItem(
            "③写作引擎",
            "带辩论简报生成图文",
            "app/modules/writing/routes.py::_article_prompt_with_brief",
            writing_prompts._article_prompt_with_brief(
                topic,
                ctx,
                style,
                brief=value("{debate_brief}", "辩论综合简报示例：推荐切入角度、结构建议、必须包含素材和配图方向。"),
                platform=article.platform,
                word_count=article.word_count,
                writing_experience=unified_experience,
            ),
            "默认注入 Campaign 总体经验包；写作侧重点会优先使用审核退回、标题结构、事实和平台语气经验。",
        ),
        PromptItem(
            "③写作引擎",
            "AI 预设写作风格",
            "app/modules/writing/routes.py::_preset_prompt",
            writing_prompts._preset_prompt(sample_campaign, ctx, 3),
        ),
        PromptItem(
            "③写作引擎",
            "联网/URL 提取写作风格",
            "app/modules/writing/routes.py::_extract_style_prompt",
            writing_prompts._extract_style_prompt(
                value("{reference_url}", "https://example.com/reference"),
                value("{reference_page_text}", "这里会注入网页正文，最多约 6000 字。"),
            ),
        ),
        PromptItem(
            "③写作引擎",
            "搜索命中提炼写作风格",
            "app/modules/writing/routes.py::_extract_from_hit_prompt",
            writing_prompts._extract_from_hit_prompt({
                "title": value("{search_hit.title}", "参考文章标题"),
                "summary": value("{search_hit.summary}", "搜索结果摘要会放在这里。"),
                "url": value("{search_hit.url}", "https://example.com/reference"),
            }),
        ),
        PromptItem(
            "③写作引擎",
            "多角色辩论 / 单角色发言",
            "app/modules/writing/debate.py::_debate_prompt",
            _debate_prompt(role_key, role_name, role_stance, topic, ctx, "（首轮，尚无前序发言）", unified_experience),
            "辩论阶段也默认读取 Campaign 总体经验包，避免重复已被验证无效的问题。",
        ),
        PromptItem(
            "③写作引擎",
            "多角色评审 / 单角色意见",
            "app/modules/writing/debate.py::_review_prompt",
            _review_prompt(role_key, role_name, role_stance, article, "（首轮，尚无前序发言）"),
        ),
        PromptItem(
            "③写作引擎",
            "按评审建议重写文章",
            "app/modules/writing/debate.py::rewrite_prompt",
            rewrite_prompt(
                article,
                value("{review_summary}", "综合评审建议示例：开头更具体，保留插图标记，压缩中段解释。"),
                topic,
                ctx,
                style.summary,
                unified_experience,
            ),
            "评审后重写也继承同一 Campaign 总体经验包。",
        ),
        PromptItem(
            "③写作引擎",
            "AI 配图 / 单个插图位",
            "app/modules/writing/routes.py::_image_prompt_for_slot",
            writing_prompts._image_prompt_for_slot(
                topic,
                ctx,
                style,
                value("{slot_desc}", "一枚写满字迹的汉简置于桌面，旁边有毛笔和边塞地图"),
                article.body + "\n[插图：" + value("{slot_desc}", "一枚写满字迹的汉简置于桌面，旁边有毛笔和边塞地图") + "]",
                platform=article.platform,
            ),
            "这是传给图像模型的画面 prompt，不是文章模型 prompt。",
        ),
        PromptItem(
            "④排期版",
            "AI 推荐排期",
            "app/modules/schedule/models.py::ScheduleSetting.recommend_prompt",
            DEFAULT_RECOMMEND_PROMPT if template else _schedule_prompt(session),
            "当前排期逻辑按这个策略做均衡分配；页面内保存后这里会同步展示最新版本。",
        ),
        PromptItem(
            "⑤数据反馈",
            "经验生成 / 选题经验",
            "app/modules/feedback/experience.py::_draft_prompt",
            _draft_prompt(sample, "选题经验"),
            "点击生成经验时会和写作经验一起生成。",
        ),
        PromptItem(
            "⑤数据反馈",
            "经验生成 / 写作经验",
            "app/modules/feedback/experience.py::_draft_prompt",
            _draft_prompt(sample, "写作经验"),
            "点击生成经验时会和选题经验一起生成。",
        ),
    ]


def _group_items(items: list[PromptItem]) -> list[SimpleNamespace]:
    groups: dict[str, list[PromptItem]] = {}
    for item in items:
        groups.setdefault(item.module, []).append(item)
    return [SimpleNamespace(name=name, items=group_items) for name, group_items in groups.items()]


@router.get("/prompts")
def prompts_home(request: Request, session: Session = Depends(get_session)):
    mode = request.query_params.get("mode", "template")
    if mode not in ("template", "preview"):
        mode = "template"
    items = _prompt_items(session, mode)
    return templates.TemplateResponse(request, "prompts/home.html", {
        "groups": _group_items(items),
        "total": len(items),
        "mode": mode,
    })
