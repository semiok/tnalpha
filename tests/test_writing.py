"""③写作引擎：Style / Article 契约、图文生成、权限与 UI。

重点守 MET-8：
- 读② Topic(status='采纳') 做待写作队列，不回写 Topic.status
- 写作状态存在③自己的 Article
- 提供 writing_status_map 给②只读
- LLM / 图片都走 core.llm 统一入口
"""
from sqlmodel import Session, select

from app.modules.knowledge.models import Brand, Campaign
from app.modules.topic.models import Topic
from app.modules.writing import routes as wroutes
from app.modules.writing.contract import writing_status_map
from app.modules.writing.models import Article, Style


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


def test_writing_status_map_reads_article_state(fresh_db):
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s)
        s.add(Article(topic_id=topic.id, campaign_id=campaign.id, title="稿件", status="图文完成"))
        s.commit()
        assert writing_status_map(s, [topic.id, 999]) == {topic.id: "图文完成"}


def test_writing_home_lists_only_adopted_topics(owner_client, fresh_db):
    with Session(fresh_db) as s:
        _seed_topic(s, status="采纳")
        _seed_topic(s, status="候选")
    html = owner_client.get("/writing").text
    assert "③写作引擎" in html
    assert "一枚汉简写了什么" in html
    assert "待写作" in html


def test_capture_styles_creates_default_styles(owner_client, fresh_db, monkeypatch):
    with Session(fresh_db) as s:
        _brand, campaign, _topic = _seed_topic(s)
        cid = campaign.id

    monkeypatch.setattr(wroutes.sources, "gather", lambda names, query, **k: [
        {"title": "公众号爆款写法", "summary": "短句开场，史料结尾", "url": "https://x/1", "source": "mp"},
        {"title": "小红书笔记", "summary": "标题有钩子，段落很短", "url": "https://x/2", "source": "google"},
    ])
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


def test_generate_article_uses_topic_context_style_and_does_not_change_topic(owner_client, fresh_db, monkeypatch):
    seen = {}

    def fake_text(prompt, task="default", module="default", **k):
        seen["text"] = {"prompt": prompt, "task": task, "module": module}
        return "标题：一枚汉简写了什么\n\n正文：这是一篇完整文章。"

    def fake_image(prompt, module="default"):
        seen["image"] = {"prompt": prompt, "module": module}
        return "/static/generated/hanjian.png"

    monkeypatch.setattr(wroutes.llm, "generate_text", fake_text)
    monkeypatch.setattr(wroutes.llm, "generate_image", fake_image)
    with Session(fresh_db) as s:
        _brand, campaign, topic = _seed_topic(s)
        s.add(Style(campaign_id=campaign.id, name="默认风格", summary="短句开场，史料收束", is_default=True))
        s.commit()
        tid = topic.id

    r = owner_client.post(f"/writing/topics/{tid}/generate", follow_redirects=False)
    assert r.status_code == 303
    assert seen["text"]["module"] == "writing"
    assert seen["image"]["module"] == "writing"
    assert "KnowledgeContext" not in seen["text"]["prompt"]
    assert "短句开场" in seen["text"]["prompt"]
    assert "悬泉置里程简" in seen["text"]["prompt"]
    with Session(fresh_db) as s:
        topic = s.get(Topic, tid)
        article = s.exec(select(Article).where(Article.topic_id == tid)).one()
        assert topic.status == "采纳"
        assert article.status == "图文完成"
        assert "完整文章" in article.body
        assert article.image_url.endswith(".png")


def test_generate_requires_editor_level(publisher_client, fresh_db):
    with Session(fresh_db) as s:
        _brand, _campaign, topic = _seed_topic(s)
        tid = topic.id
    assert publisher_client.post(f"/writing/topics/{tid}/generate").status_code == 403


def test_generate_rejects_candidate_topic(owner_client, fresh_db):
    with Session(fresh_db) as s:
        _brand, _campaign, topic = _seed_topic(s, status="候选")
        tid = topic.id
    r = owner_client.post(f"/writing/topics/{tid}/generate", follow_redirects=False)
    assert r.status_code == 400
