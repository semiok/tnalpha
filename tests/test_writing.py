"""③写作引擎：Style / Article 契约、图文生成、多角色辩论、权限与 UI。

重点守 MET-8：
- 读② Topic(status='采纳') 做待写作队列，不回写 Topic.status
- 写作状态存在③自己的 Article
- 提供 writing_status_map 给②只读
- LLM / 图片都走 core.llm 统一入口
- 多角色辩论/评审记录持久化到 DebateRecord
"""
import time

from sqlmodel import Session, select

from app.modules.knowledge.models import Brand, Campaign
from app.modules.topic.models import Topic
from app.modules.writing import routes as wroutes
from app.modules.writing.contract import writing_status_map
from app.modules.writing.models import Article, ArticleImage, DebateRecord, Style


# ── 同步线程 helper：让后台线程在 start() 时同步执行完，测试可直接断言结果 ──

class _SyncThread:
    """替身：start() 同步执行 target，无真正线程。接受 Thread 的所有 kwargs。"""
    def __init__(self, target=None, args=(), daemon=None, name=None, group=None, **kwargs):
        self._target = target
        self._args = args

    def start(self):
        if self._target:
            self._target(*self._args)

    def join(self, timeout=None):
        pass


def _patch_threading(monkeypatch):
    """只替换 routes 模块里用的 Thread 引用，不影响全局 threading。"""
    monkeypatch.setattr(wroutes.threading, "Thread", _SyncThread)
    # 全局 threading 也被影响（模块单例），anyio 会创建 Thread(name=...)，_SyncThread 兼容


# ── 等待后台线程完成（同步模式下不需要，但保留兼容） ──

def _wait_for_article(fresh_db, article_id, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with Session(fresh_db) as s:
            a = s.get(Article, article_id)
            if a and a.status not in ("辩论中", "写作中"):
                return a
        time.sleep(0.1)
    with Session(fresh_db) as s:
        return s.get(Article, article_id)


# ── seed 数据 ──

def _seed_topic(session: Session, status: str = "采纳") -> tuple[Brand, Campaign, Topic]:
    brand = Brand(
        name="敦煌",
        brand_prompt="克制、诗性、准确",
        content_notes="公众号长文，保留史料出处",
        doc_digest="敦煌与丝路的品牌资料综合",
        style_digest="冷静文博视觉，暗色展厅特写",
    )
    session.add(brand)
    session.commit()
    session.refresh(brand)
    campaign = Campaign(
        brand_id=brand.id,
        name="丝路有多长",
        campaign_digest="③选题方向：从汉简和当代装置切入丝路距离",
    )
    session.add(campaign)
    session.commit()
    session.refresh(campaign)
    topic = Topic(
        brand_id=brand.id,
        campaign_id=campaign.id,
        title="一枚汉简写了什么",
        outline="从悬泉置里程简切入，讲古人如何计量丝路里程。",
        angle="古代信息系统",
        audience="城市青年",
        materials="悬泉里程简",
        image_hint="文物暗场特写",
        publish_window="暑期研学季",
        status=status,
    )
    session.add(topic)
    session.commit()
    session.refresh(topic)
    return brand, campaign, topic


# ── 契约测试 ──

def test_writing_status_map_reads_article_state(fresh_db):
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s)
        s.add(Article(topic_id=topic.id, campaign_id=campaign.id, title="稿件", status="已生成"))
        s.commit()
        assert writing_status_map(s, [topic.id, 999]) == {topic.id: "已生成"}


def test_writing_home_lists_only_adopted_topics(owner_client, fresh_db):
    with Session(fresh_db) as s:
        _seed_topic(s, status="采纳")
        _seed_topic(s, status="候选")
    html = owner_client.get("/writing").text
    assert "③写作引擎" in html
    assert "一枚汉简写了什么" in html
    assert "待写作选题" in html


def test_writing_home_shows_adopted_topic_even_with_article(owner_client, fresh_db):
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s, status="采纳")
        s.add(Article(topic_id=topic.id, campaign_id=campaign.id, title="已生成文章", status="已生成"))
        s.commit()
    html = owner_client.get("/writing").text
    assert "待写作选题" in html
    assert "一枚汉简写了什么" in html


# ── 风格管理测试 ──

def test_capture_styles_creates_default_styles(owner_client, fresh_db, monkeypatch):
    with Session(fresh_db) as s:
        _brand, campaign, _topic = _seed_topic(s)
        cid = campaign.id

    monkeypatch.setattr(wroutes.sources, "gather", lambda names, query, **k: [
        {"title": "公众号爆款写法", "summary": "短句开场，史料结尾", "url": "https://x/1", "source": "mp"},
        {"title": "小红书笔记", "summary": "标题有钩子，段落很短", "url": "https://x/2", "source": "google"},
    ])
    def fake_llm(prompt, task="default", module="default", **k):
        if "公众号" in prompt:
            return "名称：短句开场体\n总结：短句开场，史料结尾，节奏明快。"
        return "名称：钩子标题体\n总结：标题有钩子，段落很短，适合社媒。"
    monkeypatch.setattr(wroutes.llm, "generate_text", fake_llm)
    r = owner_client.post(f"/writing/styles/campaign/{cid}/capture", data={
        "query": "敦煌 文博 写作风格",
        "source": ["mp", "google"],
        "count": "5",
    }, follow_redirects=False)
    assert r.status_code == 303
    with Session(fresh_db) as s:
        styles = s.exec(select(Style).where(Style.campaign_id == cid).order_by(Style.id)).all()
        assert len(styles) == 2
        assert styles[0].is_default is True
        assert styles[0].source == "mp"
        assert styles[0].name == "短句开场体"
        assert styles[1].source == "google"


def test_set_default_style(owner_client, fresh_db):
    with Session(fresh_db) as s:
        _brand, campaign, _topic = _seed_topic(s)
        a = Style(campaign_id=campaign.id, name="A", summary="a", is_default=True)
        b = Style(campaign_id=campaign.id, name="B", summary="b", is_default=False)
        s.add(a)
        s.add(b)
        s.commit()
        s.refresh(a)
        s.refresh(b)
        aid, bid = a.id, b.id
    r = owner_client.post(f"/writing/styles/{bid}/default", follow_redirects=False)
    assert r.status_code == 303
    with Session(fresh_db) as s:
        assert s.get(Style, aid).is_default is False
        assert s.get(Style, bid).is_default is True


# ── 文章生成测试 ──

def test_generate_no_debate_no_review(owner_client, fresh_db, monkeypatch):
    """辩论/评审轮次=0：走原始流程，同步线程。"""
    _patch_threading(monkeypatch)
    seen = {}

    def fake_text(prompt, task="default", module="default", **k):
        seen["text"] = {"prompt": prompt, "task": task, "module": module, "fallback": k.get("fallback")}
        return "标题：一枚汉简写了什么\n\n正文：这是一篇完整文章。"

    def fake_image(prompt, module="default", **k):
        seen["image"] = {"prompt": prompt, "module": module, "fallback": k.get("fallback")}
        return "/static/generated/hanjian.png"

    monkeypatch.setattr(wroutes.llm, "generate_text", fake_text)
    monkeypatch.setattr(wroutes.llm, "generate_image", fake_image)
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s)
        s.add(Style(campaign_id=campaign.id, name="默认风格", summary="短句开场，史料收束", is_default=True))
        s.commit()
        tid = topic.id

    r = owner_client.post(f"/writing/topics/{tid}/generate",
                           data={"debate_rounds": "0", "review_rounds": "0"}, follow_redirects=False)
    assert r.status_code == 303
    assert seen["text"]["module"] == "writing"
    assert seen["image"]["module"] == "writing"
    assert seen["text"]["fallback"] is False
    assert seen["image"]["fallback"] is False
    assert len(seen["image"]["prompt"]) <= 1400
    assert "短句开场" in seen["text"]["prompt"]
    assert "悬泉置里程简" in seen["text"]["prompt"]
    with Session(fresh_db) as s:
        topic = s.get(Topic, tid)
        article = s.exec(select(Article).where(Article.topic_id == tid)).one()
        assert topic.status == "采纳"
        assert article.status == "待选图"
        assert "完整文章" in article.body
        images = s.exec(select(ArticleImage).where(ArticleImage.article_id == article.id)).all()
        assert len(images) >= 1
        assert all(img.image_url.endswith(".png") for img in images)
        assert article.debate_rounds == 0
        assert article.review_rounds == 0


def test_generate_with_debate_creates_records(owner_client, fresh_db, monkeypatch):
    """辩论 2 轮 → DebateRecord 8 条（2×4角色）+ 综合简报 1 条 + 文章生成。"""
    _patch_threading(monkeypatch)
    call_count = {"text": 0, "image": 0}

    def fake_text(prompt, task="default", module="default", **k):
        call_count["text"] += 1
        if task == "debate":
            return f"这是辩论发言。"
        if task == "debate_brief":
            return "写作简报：从汉简切入，讲古代信息系统。"
        if task == "review":
            return "评审意见：结构清晰。"
        if task == "review_summary":
            return "评审摘要：保持优点，优化开头。"
        if task == "writing_rewrite":
            return "标题：重写后的标题\n\n正文：重写后的正文。"
        # article generation
        return "标题：一枚汉简写了什么\n\n正文：这是一篇完整文章。"

    def fake_image(prompt, module="default", **k):
        call_count["image"] += 1
        return "/static/generated/hanjian.png"

    monkeypatch.setattr(wroutes.llm, "generate_text", fake_text)
    monkeypatch.setattr(wroutes.llm, "generate_image", fake_image)
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s)
        tid = topic.id

    r = owner_client.post(f"/writing/topics/{tid}/generate",
                           data={"debate_rounds": "2", "review_rounds": "0"}, follow_redirects=False)
    assert r.status_code == 303
    with Session(fresh_db) as s:
        article = s.exec(select(Article).where(Article.topic_id == tid)).one()
        assert article.status == "待选图"
        assert article.debate_rounds == 2
        assert article.review_rounds == 0
        assert "写作简报" in article.debate_brief
        # 辩论记录：2 轮 × 4 角色 = 8 条
        debate_records = s.exec(
            select(DebateRecord).where(
                DebateRecord.article_id == article.id, DebateRecord.phase == "debate"
            )
        ).all()
        assert len(debate_records) == 8
        # 评审记录：0 条
        review_records = s.exec(
            select(DebateRecord).where(
                DebateRecord.article_id == article.id, DebateRecord.phase == "review"
            )
        ).all()
        assert len(review_records) == 0
        assert article.review_summary == ""


def test_generate_with_review_rewrites_article(owner_client, fresh_db, monkeypatch):
    """评审 1 轮 → 评审记录 4 条 + 按建议重写文章。"""
    _patch_threading(monkeypatch)

    def fake_text(prompt, task="default", module="default", **k):
        if task == "review":
            return "评审意见：开头不够吸引。"
        if task == "review_summary":
            return "评审摘要：优化开头钩子。"
        if task == "writing_rewrite":
            return "标题：重写标题\n\n正文：重写后的正文，开头更有钩子。"
        return "标题：原标题\n\n正文：原始正文。"

    monkeypatch.setattr(wroutes.llm, "generate_text", fake_text)
    monkeypatch.setattr(wroutes.llm, "generate_image", lambda *a, **k: "/static/generated/a.png")
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s)
        tid = topic.id

    r = owner_client.post(f"/writing/topics/{tid}/generate",
                           data={"debate_rounds": "0", "review_rounds": "1"}, follow_redirects=False)
    assert r.status_code == 303
    with Session(fresh_db) as s:
        article = s.exec(select(Article).where(Article.topic_id == tid)).one()
        assert article.status == "待选图"
        assert article.review_rounds == 1
        assert "重写后的正文" in article.body
        assert article.title == "重写标题"
        assert "评审摘要" in article.review_summary
        review_records = s.exec(
            select(DebateRecord).where(
                DebateRecord.article_id == article.id, DebateRecord.phase == "review"
            )
        ).all()
        assert len(review_records) == 4  # 1 轮 × 4 角色


def test_generate_with_debate_and_review_full_flow(owner_client, fresh_db, monkeypatch):
    """辩论 1 轮 + 评审 1 轮 → 完整流程。"""
    _patch_threading(monkeypatch)

    def fake_text(prompt, task="default", module="default", **k):
        if task == "debate":
            return "辩论发言。"
        if task == "debate_brief":
            return "写作简报。"
        if task == "review":
            return "评审意见。"
        if task == "review_summary":
            return "评审摘要。"
        if task == "writing_rewrite":
            return "标题：最终版\n\n正文：最终正文。"
        return "标题：初版\n\n正文：初版正文。"

    monkeypatch.setattr(wroutes.llm, "generate_text", fake_text)
    monkeypatch.setattr(wroutes.llm, "generate_image", lambda *a, **k: "/static/generated/a.png")
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s)
        tid = topic.id

    r = owner_client.post(f"/writing/topics/{tid}/generate",
                           data={"debate_rounds": "1", "review_rounds": "1"}, follow_redirects=False)
    assert r.status_code == 303
    with Session(fresh_db) as s:
        article = s.exec(select(Article).where(Article.topic_id == tid)).one()
        assert article.status == "待选图"
        assert article.debate_rounds == 1
        assert article.review_rounds == 1
        assert "最终正文" in article.body
        all_records = s.exec(
            select(DebateRecord).where(DebateRecord.article_id == article.id)
        ).all()
        # 1 轮辩论 × 4 + 1 轮评审 × 4 = 8
        assert len(all_records) == 8


# ── 权限测试 ──

def test_generate_requires_editor_level(publisher_client, fresh_db):
    with Session(fresh_db) as s:
        _brand, _campaign, topic = _seed_topic(s)
        tid = topic.id
    assert publisher_client.post(f"/writing/topics/{tid}/generate").status_code == 403


# ── 失败处理测试 ──

def test_generate_text_failure_persists_error(owner_client, fresh_db, monkeypatch):
    """文本 provider 失败 → error_message 存库，状态回到写作中。"""
    _patch_threading(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("文本 provider 'minimax-m3' 调用失败：401")

    monkeypatch.setattr(wroutes.llm, "generate_text", boom)
    with Session(fresh_db) as s:
        _brand, _campaign, topic = _seed_topic(s)
        tid = topic.id
    r = owner_client.post(f"/writing/topics/{tid}/generate",
                           data={"debate_rounds": "0", "review_rounds": "0"}, follow_redirects=False)
    assert r.status_code == 303  # 异步：路由立即返回 303
    with Session(fresh_db) as s:
        article = s.exec(select(Article).where(Article.topic_id == tid)).one()
        assert article.status == "写作中"
        assert "401" in article.error_message


# ── 清洗测试 ──

def test_generate_strips_model_thinking_before_save(owner_client, fresh_db, monkeypatch):
    _patch_threading(monkeypatch)
    monkeypatch.setattr(
        wroutes.llm,
        "generate_text",
        lambda *a, **k: "\u003cthought\u003einternal reasoning\u003c/thought\u003e\n标题：正式标题\n\n正文：正式文章。",
    )
    monkeypatch.setattr(wroutes.llm, "generate_image", lambda *a, **k: "https://img.example/a.png")
    with Session(fresh_db) as s:
        _brand, _campaign, topic = _seed_topic(s)
        tid = topic.id
    r = owner_client.post(f"/writing/topics/{tid}/generate",
                           data={"debate_rounds": "0", "review_rounds": "0"}, follow_redirects=False)
    assert r.status_code == 303
    with Session(fresh_db) as s:
        article = s.exec(select(Article).where(Article.topic_id == tid)).one()
        assert "internal reasoning" not in article.body
        assert article.title == "正式标题"
        assert "正式文章" in article.body


def test_generate_strips_markdown_fence_before_save(owner_client, fresh_db, monkeypatch):
    _patch_threading(monkeypatch)
    monkeypatch.setattr(
        wroutes.llm,
        "generate_text",
        lambda *a, **k: "```\n标题：正式标题\n\n正文：正式文章。\n```",
    )
    monkeypatch.setattr(wroutes.llm, "generate_image", lambda *a, **k: "https://img.example/a.png")
    with Session(fresh_db) as s:
        _brand, _campaign, topic = _seed_topic(s)
        tid = topic.id
    r = owner_client.post(f"/writing/topics/{tid}/generate",
                           data={"debate_rounds": "0", "review_rounds": "0"}, follow_redirects=False)
    assert r.status_code == 303
    with Session(fresh_db) as s:
        article = s.exec(select(Article).where(Article.topic_id == tid)).one()
        assert not article.body.startswith("```")
        assert article.title == "正式标题"


def test_generate_rejects_candidate_topic(owner_client, fresh_db):
    with Session(fresh_db) as s:
        _brand, _campaign, topic = _seed_topic(s, status="候选")
        tid = topic.id
    r = owner_client.post(f"/writing/topics/{tid}/generate", follow_redirects=False)
    assert r.status_code == 400


# ── 文章库管理测试 ──

def test_article_library_hides_deleted_by_default_and_can_restore(owner_client, fresh_db):
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s)
        article = Article(
            topic_id=topic.id,
            campaign_id=campaign.id,
            title="可删除文章",
            body="正文",
            image_url="https://img.example/a.png",
            status="已生成",
        )
        s.add(article)
        s.commit()
        s.refresh(article)
        aid = article.id
    page = owner_client.get("/writing").text
    assert "文章库" in page and "可删除文章" in page

    r = owner_client.post(f"/writing/articles/{aid}/delete", follow_redirects=False)
    assert r.status_code == 303
    # 「全部」筛选含已删除文章（恢复按钮可见）
    page = owner_client.get("/writing").text
    assert "可删除文章" in page and "恢复" in page
    deleted = owner_client.get("/writing?status=已删除").text
    assert "可删除文章" in deleted and "恢复" in deleted

    r = owner_client.post(f"/writing/articles/{aid}/restore", follow_redirects=False)
    assert r.status_code == 303
    page = owner_client.get("/writing").text
    assert "可删除文章" in page


# ── 轮询端点测试 ──

def test_generate_status_returns_progress_for_running_article(owner_client, fresh_db):
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s)
        article = Article(topic_id=topic.id, campaign_id=campaign.id, title="进行中", status="辩论中")
        s.add(article)
        s.commit()
        s.refresh(article)
        aid = article.id
        s.add(DebateRecord(article_id=aid, phase="debate", round_num=1, role="writer", content="主笔发言"))
        s.commit()
    r = owner_client.get(f"/writing/articles/{aid}/generate-status")
    assert "辩论中" in r.text or "主笔" in r.text
    assert f"article-row-{aid}" in r.text


def test_generate_status_returns_final_card_when_done(owner_client, fresh_db):
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s)
        article = Article(
            topic_id=topic.id, campaign_id=campaign.id, title="已完成",
            body="正文", image_url="https://img.example/a.png", status="已生成"
        )
        s.add(article)
        s.commit()
        s.refresh(article)
        aid = article.id
    r = owner_client.get(f"/writing/articles/{aid}/generate-status", follow_redirects=False)
    assert r.status_code == 200
    assert "article-row-" in r.text
    assert "已完成" in r.text


def test_generate_htmx_returns_topic_card_and_oob_generating_card(owner_client, fresh_db, monkeypatch):
    """HTMX 点击生成图文：左侧刷新选题卡片 + 右侧 OOB 追加生成中卡片。"""
    _patch_threading(monkeypatch)
    monkeypatch.setattr(wroutes.llm, "generate_text", lambda *a, **k: "标题：T\n\n正文：B")
    monkeypatch.setattr(wroutes.llm, "generate_image", lambda *a, **k: "https://img.example/a.png")
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s)
        tid = topic.id

    r = owner_client.post(
        f"/writing/topics/{tid}/generate",
        data={"debate_rounds": "0", "review_rounds": "0"},
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    # 主响应包含左侧选题卡片
    assert f'id="topic-card-{tid}"' in r.text
    # OOB 追加右侧列表项
    assert 'hx-swap-oob="beforeend:#article-list"' in r.text
    assert "article-row-" in r.text
