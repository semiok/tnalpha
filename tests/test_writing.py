"""③写作引擎：Style / Article 契约、图文生成、多角色辩论、权限与 UI。

重点守 MET-8：
- 读② Topic(status='采纳') 做待写作队列，不回写 Topic.status
- 写作状态存在③自己的 Article
- 提供 writing_status_map 给②只读
- LLM / 图片都走 core.llm 统一入口
- 多角色辩论/评审记录持久化到 DebateRecord
"""
import io
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
        s.add(Article(topic_id=topic.id, campaign_id=campaign.id, title="稿件", status="待审核"))
        s.commit()
        assert writing_status_map(s, [topic.id, 999]) == {topic.id: "待审核"}


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
        s.add(Article(topic_id=topic.id, campaign_id=campaign.id, title="待审核文章", status="待审核"))
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

    def fake_images(prompt, module="default", n=4, **k):
        seen["image"] = {"prompt": prompt, "module": module, "fallback": k.get("fallback")}
        return ["/static/generated/hanjian.png"] * n

    monkeypatch.setattr(wroutes.llm, "generate_text", fake_text)
    monkeypatch.setattr(wroutes.llm, "generate_images", fake_images)
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s)
        s.add(Style(campaign_id=campaign.id, name="默认风格", summary="短句开场，史料收束", is_default=True))
        s.commit()
        tid = topic.id

    r = owner_client.post(f"/writing/topics/{tid}/generate",
                           data={"debate_rounds": "0", "review_rounds": "0", "ai_images": "true"}, follow_redirects=False)
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
        assert article.status == "待审核"
        assert "完整文章" in article.body
        images = s.exec(select(ArticleImage).where(ArticleImage.article_id == article.id)).all()
        assert len(images) >= 1
        assert all(img.image_url.endswith(".png") for img in images)
        assert article.debate_rounds == 0
        assert article.review_rounds == 0


def test_regenerate_clears_old_candidate_images(owner_client, fresh_db, monkeypatch):
    """重新生成（含未勾 AI 配图）时，旧候选图与主图字段必须被清空，避免与新正文错位残留。"""
    _patch_threading(monkeypatch)
    call_count = {"img": 0}

    def fake_text(prompt, task="default", module="default", **k):
        return "标题：一枚汉简写了什么\n\n正文：这是一篇完整文章。"

    def fake_images(prompt, module="default", n=4, **k):
        call_count["img"] += 1
        return [f"/static/generated/r{call_count['img']}-{i}.png" for i in range(n)]

    monkeypatch.setattr(wroutes.llm, "generate_text", fake_text)
    monkeypatch.setattr(wroutes.llm, "generate_images", fake_images)
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s)
        s.add(Style(campaign_id=campaign.id, name="默认风格", summary="短句开场", is_default=True))
        s.commit()
        tid = topic.id

    # 第一次：勾 AI 配图生成，留下候选图 + 主图
    owner_client.post(f"/writing/topics/{tid}/generate",
                      data={"debate_rounds": "0", "review_rounds": "0", "ai_images": "true"},
                      follow_redirects=False)
    with Session(fresh_db) as s:
        article = s.exec(select(Article).where(Article.topic_id == tid)).one()
        aid = article.id
        old_count = s.exec(select(ArticleImage).where(ArticleImage.article_id == aid)).all()
        assert len(old_count) >= 1
        article.image_url = "/writing/uploads/old.jpg"
        article.image_prompt = "手动上传"
        s.add(article)
        s.commit()

    # 第二次：不勾 AI 配图重新生成（checkbox 未勾选 → ai_images 不发送）
    owner_client.post(f"/writing/topics/{tid}/generate",
                      data={"debate_rounds": "0", "review_rounds": "0", "ai_images": ""},
                      follow_redirects=False)
    with Session(fresh_db) as s:
        article = s.get(Article, aid)
        assert article.status == "待审核"
        assert article.image_url == ""
        assert article.image_prompt == ""
        remaining = s.exec(select(ArticleImage).where(ArticleImage.article_id == aid)).all()
        assert remaining == [], f"重新生成后旧候选图应被清空，但剩 {len(remaining)} 张"


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

    def fake_images(prompt, module="default", n=4, **k):
        call_count["image"] += 1
        return ["/static/generated/hanjian.png"] * n

    monkeypatch.setattr(wroutes.llm, "generate_text", fake_text)
    monkeypatch.setattr(wroutes.llm, "generate_images", fake_images)
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s)
        tid = topic.id

    r = owner_client.post(f"/writing/topics/{tid}/generate",
                           data={"debate_rounds": "2", "review_rounds": "0"}, follow_redirects=False)
    assert r.status_code == 303
    with Session(fresh_db) as s:
        article = s.exec(select(Article).where(Article.topic_id == tid)).one()
        assert article.status == "待审核"
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
    monkeypatch.setattr(wroutes.llm, "generate_images", lambda *a, **k: ["/static/generated/a.png"] * k.get("n", 4))
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s)
        tid = topic.id

    r = owner_client.post(f"/writing/topics/{tid}/generate",
                           data={"debate_rounds": "0", "review_rounds": "1"}, follow_redirects=False)
    assert r.status_code == 303
    with Session(fresh_db) as s:
        article = s.exec(select(Article).where(Article.topic_id == tid)).one()
        assert article.status == "待审核"
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
    monkeypatch.setattr(wroutes.llm, "generate_images", lambda *a, **k: ["/static/generated/a.png"] * k.get("n", 4))
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s)
        tid = topic.id

    r = owner_client.post(f"/writing/topics/{tid}/generate",
                           data={"debate_rounds": "1", "review_rounds": "1"}, follow_redirects=False)
    assert r.status_code == 303
    with Session(fresh_db) as s:
        article = s.exec(select(Article).where(Article.topic_id == tid)).one()
        assert article.status == "待审核"
        assert article.debate_rounds == 1
        assert article.review_rounds == 1
        assert "最终正文" in article.body
        all_records = s.exec(
            select(DebateRecord).where(DebateRecord.article_id == article.id)
        ).all()
        # 1 轮辩论 × 4 + 1 轮评审 × 4 = 8
        assert len(all_records) == 8


def test_multi_slot_images_generate_four_candidates_and_select_per_slot(owner_client, fresh_db, monkeypatch):
    """每个插图提示词生成 4 张候选图；提交时每个 slot 可独立选择一张。"""
    _patch_threading(monkeypatch)
    calls = {"image": 0}

    monkeypatch.setattr(
        wroutes.llm,
        "generate_text",
        lambda *a, **k: "标题：多图稿\n\n正文：第一段。\n[插图：汉简暗场特写]\n第二段。\n[插图：丝路地图装置]\n结尾。",
    )

    def fake_images(prompt, module="default", n=4, **k):
        calls["image"] += 1
        return [f"https://img.example/{calls['image']}-{i}.png" for i in range(n)]

    monkeypatch.setattr(wroutes.llm, "generate_images", fake_images)
    with Session(fresh_db) as s:
        _brand, _campaign, topic = _seed_topic(s)
        tid = topic.id

    r = owner_client.post(
        f"/writing/topics/{tid}/generate",
        data={"debate_rounds": "0", "review_rounds": "0", "ai_images": "true"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with Session(fresh_db) as s:
        article = s.exec(select(Article).where(Article.topic_id == tid)).one()
        aid = article.id
        images = s.exec(
            select(ArticleImage).where(ArticleImage.article_id == aid).order_by(ArticleImage.slot_index, ArticleImage.id)
        ).all()
        assert len(images) == 8
        assert [len([img for img in images if img.slot_index == idx]) for idx in (0, 1)] == [4, 4]
        assert len([img for img in images if img.is_selected]) == 2
        second_by_slot = {
            idx: [img for img in images if img.slot_index == idx][1].id
            for idx in (0, 1)
        }

    r = owner_client.post(
        f"/writing/articles/{aid}/select-images",
        data={"image_id_0": str(second_by_slot[0]), "image_id_1": str(second_by_slot[1])},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with Session(fresh_db) as s:
        article = s.get(Article, aid)
        selected = s.exec(
            select(ArticleImage).where(ArticleImage.article_id == aid, ArticleImage.is_selected == True)
            .order_by(ArticleImage.slot_index)
        ).all()
        assert article.status == "待审核"
        assert [img.id for img in selected] == [second_by_slot[0], second_by_slot[1]]
        assert article.image_url == selected[0].image_url


def test_detail_renders_fallback_image_slot_when_body_has_no_marker(owner_client, fresh_db):
    """正文没插图标记时，后端兜底生成的候选图仍会显示在详情页。"""
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s)
        article = Article(
            topic_id=topic.id,
            campaign_id=campaign.id,
            title="无标记正文",
            body="正文没有插图标记，但仍需要展示候选图。",
            status="待审核",
        )
        s.add(article)
        s.commit()
        s.refresh(article)
        for i in range(4):
            s.add(ArticleImage(
                article_id=article.id,
                prompt="文章配图",
                image_url=f"https://img.example/fallback-{i}.png",
                slot_index=0,
                slot_desc="文章配图",
                is_selected=(i == 0),
            ))
        s.commit()
        aid = article.id

    html = owner_client.get(f"/writing/articles/{aid}").text
    assert "插图位置 1" in html
    assert "fallback-0.png" in html
    assert 'name="image_id_0"' in html
    assert "确认选图" not in html
    assert f'hx-post="/writing/articles/{aid}/slots/0/select"' in html
    assert "@dblclick" in html


def test_regenerate_slot_replaces_only_that_slot(owner_client, fresh_db, monkeypatch):
    """单个提示词可重新生成 4 张候选图，其他提示词候选图保留。"""
    _patch_threading(monkeypatch)
    calls = {"image": 0}

    def fake_images(prompt, module="default", n=4, **k):
        calls["image"] += 1
        return [f"https://img.example/new-{calls['image']}-{i}.png" for i in range(n)]

    monkeypatch.setattr(wroutes.llm, "generate_images", fake_images)
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s)
        article = Article(
            topic_id=topic.id,
            campaign_id=campaign.id,
            title="多图稿",
            body="正文\n[插图：旧提示一]\n正文\n[插图：旧提示二]",
            status="待审核",
        )
        s.add(article)
        s.commit()
        s.refresh(article)
        for slot_idx in (0, 1):
            for i in range(4):
                s.add(ArticleImage(
                    article_id=article.id,
                    prompt=f"old-{slot_idx}",
                    image_url=f"https://img.example/old-{slot_idx}-{i}.png",
                    slot_index=slot_idx,
                    slot_desc=f"旧提示{slot_idx}",
                    is_selected=(i == 0),
                ))
        s.commit()
        aid = article.id

    r = owner_client.post(f"/writing/articles/{aid}/slots/1/regenerate", follow_redirects=False)
    assert r.status_code == 303
    with Session(fresh_db) as s:
        images = s.exec(
            select(ArticleImage).where(ArticleImage.article_id == aid).order_by(ArticleImage.slot_index, ArticleImage.id)
        ).all()
        slot0 = [img.image_url for img in images if img.slot_index == 0]
        slot1 = [img.image_url for img in images if img.slot_index == 1]
        assert slot0 == [f"https://img.example/old-0-{i}.png" for i in range(4)]
        assert len(slot1) == 4
        assert all("/new-" in url for url in slot1)


def test_select_slot_image_updates_only_that_slot(owner_client, fresh_db):
    """选择某一组候选图会立即保存该组选择，不要求其他组一起确认。"""
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s)
        article = Article(
            topic_id=topic.id,
            campaign_id=campaign.id,
            title="逐组选图",
            body="正文\n[插图：位置一]\n正文\n[插图：位置二]",
            status="待审核",
        )
        s.add(article)
        s.commit()
        s.refresh(article)
        ids: dict[tuple[int, int], int] = {}
        for slot_idx in (0, 1):
            for i in range(4):
                img = ArticleImage(
                    article_id=article.id,
                    prompt=f"slot-{slot_idx}",
                    image_url=f"https://img.example/{slot_idx}-{i}.png",
                    slot_index=slot_idx,
                    slot_desc=f"位置{slot_idx}",
                    is_selected=(i == 0),
                )
                s.add(img)
                s.commit()
                s.refresh(img)
                ids[(slot_idx, i)] = img.id
        aid = article.id

    r = owner_client.post(
        f"/writing/articles/{aid}/slots/1/select",
        data={"image_id": str(ids[(1, 2)])},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "确认选图" not in r.text
    with Session(fresh_db) as s:
        article = s.get(Article, aid)
        selected = s.exec(
            select(ArticleImage).where(ArticleImage.article_id == aid, ArticleImage.is_selected == True)
            .order_by(ArticleImage.slot_index, ArticleImage.id)
        ).all()
        assert article.status == "待审核"
        assert [img.id for img in selected] == [ids[(0, 0)], ids[(1, 2)]]
        assert article.image_url == "https://img.example/0-0.png"


def test_select_slot_can_change_multiple_times_before_confirm(owner_client, fresh_db):
    """单个 slot 换选保持「待审核」，用户可反复换选其他图片。"""
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s)
        article = Article(
            topic_id=topic.id,
            campaign_id=campaign.id,
            title="反复换选",
            body="正文\n[插图：位置一]\n正文\n[插图：位置二]",
            status="待审核",
        )
        s.add(article)
        s.commit()
        s.refresh(article)
        ids: dict[tuple[int, int], int] = {}
        for slot_idx in (0, 1):
            for i in range(4):
                img = ArticleImage(
                    article_id=article.id,
                    prompt=f"slot-{slot_idx}",
                    image_url=f"https://img.example/{slot_idx}-{i}.png",
                    slot_index=slot_idx,
                    slot_desc=f"位置{slot_idx}",
                    is_selected=(i == 0),
                )
                s.add(img)
                s.commit()
                s.refresh(img)
                ids[(slot_idx, i)] = img.id
        aid = article.id

    # 第一次换选 slot 0 的第 1 张
    r = owner_client.post(
        f"/writing/articles/{aid}/slots/0/select",
        data={"image_id": str(ids[(0, 1)])},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    with Session(fresh_db) as s:
        article = s.get(Article, aid)
        assert article.status == "待审核"  # 换选不改变状态

    # 再次换选 slot 0 的第 2 张（验证可反复换选）
    r = owner_client.post(
        f"/writing/articles/{aid}/slots/0/select",
        data={"image_id": str(ids[(0, 2)])},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    with Session(fresh_db) as s:
        article = s.get(Article, aid)
        assert article.status == "待审核"
        selected = s.exec(
            select(ArticleImage).where(ArticleImage.article_id == aid, ArticleImage.is_selected == True)
            .order_by(ArticleImage.slot_index)
        ).all()
        assert [img.id for img in selected] == [ids[(0, 2)], ids[(1, 0)]]

    # 换选 slot 1
    r = owner_client.post(
        f"/writing/articles/{aid}/slots/1/select",
        data={"image_id": str(ids[(1, 3)])},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    with Session(fresh_db) as s:
        article = s.get(Article, aid)
        assert article.status == "待审核"  # 仍保持待审核
        assert article.image_url == "https://img.example/0-2.png"


def test_parse_image_slots_accepts_marker_variants():
    """_parse_image_slots 兼容 LLM 常见变体：[插图：]、[插图位：]、[插图位置：]。"""
    body = (
        "正文一\n"
        "[插图：第一处描述]\n"
        "正文二\n"
        "[插图位：第二处描述]\n"
        "正文三\n"
        "[插图位置：第三处描述]\n"
        "结尾"
    )
    slots = wroutes._parse_image_slots(body)
    assert len(slots) == 3
    assert [desc for _, desc in slots] == ["第一处描述", "第二处描述", "第三处描述"]
    # _strip_image_slots 也要兼容
    stripped = wroutes._strip_image_slots(body)
    assert "[插图" not in stripped
    assert "第一处描述" not in stripped
    assert "正文一" in stripped
    assert "结尾" in stripped


def test_upload_slot_image_replaces_ai_candidates(owner_client, fresh_db):
    """手动上传图片 = 该位置最终用图：清除所有 AI 候选，只留这一张上传图。"""
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s)
        article = Article(
            topic_id=topic.id,
            campaign_id=campaign.id,
            title="上传替换",
            body="正文\n[插图：位置一]",
            status="待审核",
        )
        s.add(article)
        s.commit()
        s.refresh(article)
        for i in range(4):
            s.add(ArticleImage(
                article_id=article.id,
                prompt="old-ai",
                image_url=f"https://img.example/old-{i}.png",
                slot_index=0,
                slot_desc="位置一",
                is_selected=(i == 0),
            ))
        s.commit()
        aid = article.id

    r = owner_client.post(
        f"/writing/articles/{aid}/slots/0/upload",
        files={"file": ("manual.png", io.BytesIO(b"manual-image"), "image/png")},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "manual.png" not in r.text  # 文件名已随机化
    # 上传后保持编辑状态（force_editing=True）
    assert "editing: true" in r.text
    with Session(fresh_db) as s:
        images = s.exec(
            select(ArticleImage).where(ArticleImage.article_id == aid, ArticleImage.slot_index == 0)
            .order_by(ArticleImage.id)
        ).all()
        selected = [img for img in images if img.is_selected]
        # 只留上传图，AI 候选全删
        assert len(images) == 1
        assert len(selected) == 1
        assert selected[0].prompt == "手动上传"
        assert selected[0].image_url.startswith("/writing/uploads/writing/articles/")
        upload_url = selected[0].image_url

    file_r = owner_client.get(upload_url)
    assert file_r.status_code == 200
    assert file_r.content == b"manual-image"


def test_generated_article_images_can_be_replaced_by_upload(owner_client, fresh_db):
    """待审核文章详情页也保留手动上传入口，方便后续替换不满意的 AI 图。"""
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s)
        article = Article(
            topic_id=topic.id,
            campaign_id=campaign.id,
            title="已完成图文",
            body="正文\n[插图：位置一]",
            status="待审核",
            image_url="https://img.example/final.png",
        )
        s.add(article)
        s.commit()
        s.refresh(article)
        s.add(ArticleImage(
            article_id=article.id,
            prompt="ai",
            image_url="https://img.example/final.png",
            slot_index=0,
            slot_desc="位置一",
            is_selected=True,
        ))
        s.commit()
        aid = article.id

    html = owner_client.get(f"/writing/articles/{aid}").text
    assert f'hx-post="/writing/articles/{aid}/slots/0/upload"' in html
    assert "@dblclick" in html


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
    monkeypatch.setattr(wroutes.llm, "generate_images", lambda *a, **k: ["https://img.example/a.png"] * k.get("n", 4))
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
    monkeypatch.setattr(wroutes.llm, "generate_images", lambda *a, **k: ["https://img.example/a.png"] * k.get("n", 4))
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
            status="待审核",
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
    assert "生成中" in r.text or "主笔" in r.text
    assert f"article-row-{aid}" in r.text


def test_generate_status_returns_final_card_when_done(owner_client, fresh_db):
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s)
        article = Article(
            topic_id=topic.id, campaign_id=campaign.id, title="已完成",
            body="正文", image_url="https://img.example/a.png", status="待审核"
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
    monkeypatch.setattr(wroutes.llm, "generate_images", lambda *a, **k: ["https://img.example/a.png"] * k.get("n", 4))
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


# ── 待审核下编辑正文 + 自定义插图位置 测试 ──

def test_edit_body_updates_article_and_keeps_markers(owner_client, fresh_db):
    """待审核下保存编辑后的正文，[插图：...] 标记保留，slot_index 重排正确。"""
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s)
        article = Article(
            topic_id=topic.id, campaign_id=campaign.id,
            title="原标题",
            body="第一段\n[插图：位置一]\n第二段\n[插图：位置二]",
            status="待审核",
        )
        s.add(article); s.commit(); s.refresh(article)
        # 两 slot 各 1 张候选图
        for idx in (0, 1):
            s.add(ArticleImage(
                article_id=article.id, prompt=f"p{idx}",
                image_url=f"https://img.example/{idx}.png",
                slot_index=idx, slot_desc=f"位置{'一二'[idx]}",
                is_selected=True,
            ))
        s.commit()
        aid = article.id

    new_body = "改过的第一段\n[插图：位置一]\n改过的第二段\n[插图：位置二]"
    r = owner_client.post(
        f"/writing/articles/{aid}/edit-body",
        data={"body": new_body, "title": "新标题"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    with Session(fresh_db) as s:
        a = s.get(Article, aid)
        assert a.body == new_body
        assert a.title == "新标题"
        # slot_index 仍为 0,1（标记顺序未变）
        imgs = s.exec(select(ArticleImage).where(ArticleImage.article_id == aid).order_by(ArticleImage.slot_index)).all()
        assert [im.slot_index for im in imgs] == [0, 1]


def test_edit_body_reindexes_slots_when_marker_order_changes(owner_client, fresh_db):
    """编辑正文导致标记顺序变化时，已存候选图 slot_index 重排对齐新顺序。"""
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s)
        article = Article(
            topic_id=topic.id, campaign_id=campaign.id, title="T",
            body="正文A\n[插图：位置一]\n正文B\n[插图：位置二]",
            status="待审核",
        )
        s.add(article); s.commit(); s.refresh(article)
        s.add(ArticleImage(article_id=article.id, prompt="p1", image_url="https://img.example/1.png",
                           slot_index=0, slot_desc="位置一", is_selected=True))
        s.add(ArticleImage(article_id=article.id, prompt="p2", image_url="https://img.example/2.png",
                           slot_index=1, slot_desc="位置二", is_selected=False))
        s.commit()
        aid = article.id

    # 删掉第一个标记，只剩第二个 → 原 slot_index=1 应重排为 0
    new_body = "正文A正文B\n[插图：位置二]"
    r = owner_client.post(
        f"/writing/articles/{aid}/edit-body",
        data={"body": new_body},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    with Session(fresh_db) as s:
        imgs = s.exec(select(ArticleImage).where(ArticleImage.article_id == aid).order_by(ArticleImage.id)).all()
        assert len(imgs) == 1
        assert imgs[0].slot_index == 0
        assert imgs[0].slot_desc == "位置二"


def test_edit_body_rejects_empty_body(owner_client, fresh_db):
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s)
        article = Article(topic_id=topic.id, campaign_id=campaign.id, title="T", body="原正文", status="待审核")
        s.add(article); s.commit(); s.refresh(article)
        aid = article.id
    r = owner_client.post(f"/writing/articles/{aid}/edit-body", data={"body": "  "})
    assert r.status_code == 400


def test_edit_body_rejects_when_status_not_editable(owner_client, fresh_db):
    """只有待审核可以编辑正文，其他状态（含待配图）都拒绝。"""
    for status in ("已发布", "待配图", "写作中", "已排期"):
        with Session(fresh_db) as s:
            _brand, campaign, topic = _seed_topic(s)
            article = Article(topic_id=topic.id, campaign_id=campaign.id, title="T", body="原正文", status=status)
            s.add(article); s.commit(); s.refresh(article)
            aid = article.id
        r = owner_client.post(f"/writing/articles/{aid}/edit-body", data={"body": "新正文"})
        assert r.status_code == 400, f"状态 {status} 应拒绝编辑"


def test_insert_placeholder_at_cursor_position(owner_client, fresh_db):
    """光标位置插入占位：insert_position 指定字符偏移量，在正文中间拆分段落插入。"""
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s)
        article = Article(
            topic_id=topic.id, campaign_id=campaign.id, title="T",
            body="第一段正文\n\n第二段正文",
            status="待审核",
        )
        s.add(article); s.commit(); s.refresh(article)
        aid = article.id

    # insert_position=3 → 在"第一段"之后（偏移量 3 = "段"字后的位置）插入
    r = owner_client.post(
        f"/writing/articles/{aid}/insert-placeholder",
        data={"insert_position": "3", "anchor_text": ""},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    with Session(fresh_db) as s:
        a = s.get(Article, aid)
        assert "[插图：待选择]" in a.body
        # 标记在"第一段"之后，"正文"之前（光标位置拆分段落）
        assert a.body.index("第一段") < a.body.index("[插图：待选择]")
        assert a.body.index("[插图：待选择]") < a.body.index("正文")
        # 第二段在标记后
        assert a.body.index("[插图：待选择]") < a.body.index("第二段正文")


def test_insert_placeholder_at_paragraph_end(owner_client, fresh_db):
    """无光标位置时，用 anchor_text 在段落后插入占位符。"""
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s)
        article = Article(
            topic_id=topic.id, campaign_id=campaign.id, title="T",
            body="第一段正文\n\n第二段正文",
            status="待审核",
        )
        s.add(article); s.commit(); s.refresh(article)
        aid = article.id

    r = owner_client.post(
        f"/writing/articles/{aid}/insert-placeholder",
        data={"anchor_text": "第一段正文"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "editing: true" in r.text
    with Session(fresh_db) as s:
        a = s.get(Article, aid)
        assert "[插图：待选择]" in a.body
        assert a.body.index("第一段正文") < a.body.index("[插图：待选择]")
        assert a.body.index("[插图：待选择]") < a.body.index("第二段正文")
        imgs = s.exec(select(ArticleImage).where(ArticleImage.article_id == aid)).all()
        assert len(imgs) == 0


def test_insert_placeholder_preserves_edited_body(owner_client, fresh_db):
    """编辑模式下用户改了正文但没点保存就插占位：提交的 body 必须被保存。"""
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s)
        article = Article(
            topic_id=topic.id, campaign_id=campaign.id, title="原标题",
            body="旧的第一段\n\n旧的第二段",
            status="待审核",
        )
        s.add(article); s.commit(); s.refresh(article)
        aid = article.id

    edited_body = "用户改过的第一段\n\n用户改过的第二段"
    r = owner_client.post(
        f"/writing/articles/{aid}/insert-placeholder",
        data={
            "anchor_text": "用户改过的第一段",
            "body": edited_body,
            "title": "用户改过的标题",
        },
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    with Session(fresh_db) as s:
        a = s.get(Article, aid)
        assert "用户改过的第一段" in a.body
        assert "用户改过的第二段" in a.body
        assert "旧的第一段" not in a.body
        assert a.title == "用户改过的标题"
        assert "[插图：待选择]" in a.body


def test_upload_to_placeholder_updates_marker_and_creates_image(owner_client, fresh_db):
    """占位符选图后：标记从"待选择"改为"手动上传"，新建选中候选图。"""
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s)
        article = Article(
            topic_id=topic.id, campaign_id=campaign.id, title="T",
            body="第一段\n[插图：待选择]\n第二段",
            status="待审核",
        )
        s.add(article); s.commit(); s.refresh(article)
        aid = article.id

    r = owner_client.post(
        f"/writing/articles/{aid}/slots/0/upload",
        files={"file": ("up.png", io.BytesIO(b"img"), "image/png")},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "editing: true" in r.text
    with Session(fresh_db) as s:
        a = s.get(Article, aid)
        # 标记从"待选择"改为"手动上传"
        assert "[插图：手动上传]" in a.body
        assert "[插图：待选择]" not in a.body
        imgs = s.exec(select(ArticleImage).where(ArticleImage.article_id == aid)).all()
        assert len(imgs) == 1
        assert imgs[0].prompt == "手动上传"
        assert imgs[0].is_selected is True
        assert imgs[0].slot_desc == "待选择"  # slot_desc 保持原值
        assert a.image_url == imgs[0].image_url


def test_insert_placeholder_between_existing_slots_reindexes(owner_client, fresh_db):
    """已有 2 个 AI slot，在第一段后插占位 → 新占位成为 slot 0，原 slot 顺移。"""
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s)
        article = Article(
            topic_id=topic.id, campaign_id=campaign.id, title="T",
            body="第一段\n[插图：位置一]\n第二段\n[插图：位置二]",
            status="待审核",
        )
        s.add(article); s.commit(); s.refresh(article)
        for slot_idx, desc in ((0, "位置一"), (1, "位置二")):
            s.add(ArticleImage(article_id=article.id, prompt=f"ai{desc}",
                               image_url=f"https://img.example/ai{desc}.png",
                               slot_index=slot_idx, slot_desc=desc, is_selected=True))
        s.commit()
        aid = article.id

    r = owner_client.post(
        f"/writing/articles/{aid}/insert-placeholder",
        data={"anchor_text": "第一段"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    with Session(fresh_db) as s:
        a = s.get(Article, aid)
        # body 中标记顺序：待选择 在前，位置一、位置二在后
        assert a.body.index("[插图：待选择]") < a.body.index("[插图：位置一]")
        imgs = s.exec(select(ArticleImage).where(ArticleImage.article_id == aid).order_by(ArticleImage.slot_index, ArticleImage.id)).all()
        # slot 0 = 原"位置一"（被重排），slot 1 = 原"位置二"
        # 占位符无候选图，不影响已有图的 slot_index 重排
        assert imgs[0].slot_index == 1 and imgs[0].slot_desc == "位置一"
        assert imgs[1].slot_index == 2 and imgs[1].slot_desc == "位置二"


def test_delete_slot_removes_marker_and_images(owner_client, fresh_db):
    """删除插图位置：移除正文标记 + 删除该 slot 所有候选图 + 重排剩余 slot。"""
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s)
        article = Article(
            topic_id=topic.id, campaign_id=campaign.id, title="T",
            body="第一段\n[插图：位置一]\n第二段\n[插图：位置二]\n第三段",
            status="待审核",
        )
        s.add(article); s.commit(); s.refresh(article)
        for slot_idx, desc in ((0, "位置一"), (1, "位置二")):
            s.add(ArticleImage(article_id=article.id, prompt=f"ai{desc}",
                               image_url=f"https://img.example/ai{desc}.png",
                               slot_index=slot_idx, slot_desc=desc, is_selected=True))
        s.commit()
        aid = article.id

    # 删除 slot 0（位置一）
    r = owner_client.post(
        f"/writing/articles/{aid}/slots/0/delete",
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    with Session(fresh_db) as s:
        a = s.get(Article, aid)
        # 标记被移除
        assert "[插图：位置一]" not in a.body
        assert "[插图：位置二]" in a.body
        # 剩余图重排：原 slot 1 → slot 0
        imgs = s.exec(select(ArticleImage).where(ArticleImage.article_id == aid).order_by(ArticleImage.slot_index)).all()
        assert len(imgs) == 1
        assert imgs[0].slot_index == 0
        assert imgs[0].slot_desc == "位置二"


def test_edit_body_preserves_manual_upload_selected(owner_client, fresh_db):
    """编辑正文保存后，手动上传 slot 的 is_selected 保持不变（不丢选中状态）。"""
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s)
        article = Article(
            topic_id=topic.id, campaign_id=campaign.id, title="T",
            body="第一段\n[插图：手动上传]\n第二段",
            status="待审核",
        )
        s.add(article); s.commit(); s.refresh(article)
        s.add(ArticleImage(article_id=article.id, prompt="手动上传",
                           image_url="https://img.example/up.png",
                           slot_index=0, slot_desc="手动上传", is_selected=True))
        s.commit()
        aid = article.id

    # 编辑正文（标记不变，只改文字）
    new_body = "改过的第一段\n[插图：手动上传]\n改过的第二段"
    r = owner_client.post(
        f"/writing/articles/{aid}/edit-body",
        data={"body": new_body},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    # 保存后退出编辑模式
    assert "editing: false" in r.text
    with Session(fresh_db) as s:
        imgs = s.exec(select(ArticleImage).where(ArticleImage.article_id == aid)).all()
        assert len(imgs) == 1
        assert imgs[0].prompt == "手动上传"
        assert imgs[0].is_selected is True  # 选中状态保持
        assert imgs[0].slot_index == 0


def test_detail_page_shows_edit_button_when_pending_review(owner_client, fresh_db):
    """待审核状态详情页渲染编辑正文按钮 + 段落间插图按钮。"""
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s)
        article = Article(
            topic_id=topic.id, campaign_id=campaign.id, title="T",
            body="第一段\n[插图：位置一]\n第二段",
            status="待审核",
        )
        s.add(article); s.commit(); s.refresh(article)
        s.add(ArticleImage(article_id=article.id, prompt="ai", image_url="https://img.example/ai.png",
                           slot_index=0, slot_desc="位置一", is_selected=True))
        s.commit()
        aid = article.id

    html = owner_client.get(f"/writing/articles/{aid}").text
    assert "编辑正文" in html
    assert "/edit-body" in html
    assert "/insert-placeholder" in html
    assert "图片占位" in html
    assert 'data-seg' in html


def test_detail_page_manual_upload_slot_shows_single_image(owner_client, fresh_db):
    """手动上传 slot 详情页只显示单图，不显示候选网格/重生按钮/张数。"""
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s)
        article = Article(
            topic_id=topic.id, campaign_id=campaign.id, title="T",
            body="第一段\n[插图：手动上传]\n第二段",
            status="待审核",
        )
        s.add(article); s.commit(); s.refresh(article)
        s.add(ArticleImage(article_id=article.id, prompt="手动上传",
                           image_url="https://img.example/up.png",
                           slot_index=0, slot_desc="手动上传", is_selected=True))
        s.commit()
        aid = article.id

    html = owner_client.get(f"/writing/articles/{aid}").text
    assert "用户上传" in html          # 标签
    assert "重新上传" in html          # 按钮文案
    assert "🔄 重生本位置" not in html  # 无重生按钮（带 emoji 区分提示文案）
    assert "/4 张" not in html        # 无候选张数


def test_detail_page_no_edit_button_when_published(owner_client, fresh_db):
    """已发布状态不显示编辑入口。"""
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s)
        article = Article(
            topic_id=topic.id, campaign_id=campaign.id, title="T",
            body="正文", status="已发布", image_url="https://img.example/a.png",
        )
        s.add(article); s.commit(); s.refresh(article)
        aid = article.id
    html = owner_client.get(f"/writing/articles/{aid}").text
    assert "编辑正文" not in html
    # 按钮文案只在待审核 + level>=1 时渲染
    assert "图片占位" not in html
    assert "点击选择图片" not in html
