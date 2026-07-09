"""⑥权限矩阵：四账号按模块可见/可写。"""
from sqlmodel import Session

from app.modules.knowledge.models import Brand, Campaign
from app.modules.topic.models import Topic
from app.modules.writing.models import Article


def test_login_accounts_use_admin_password(anon_client):
    for username in ("admin0", "admin", "admin1", "admin2"):
        r = anon_client.post("/login", data={"username": username, "password": "admin@321"}, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/"


def test_admin0_can_see_all_modules_and_model_config(admin0_client):
    html = admin0_client.get("/").text
    for label in ("①知识库", "②选题库", "③写作引擎", "④排期版", "⑤数据反馈", "⑥权限", "⑦提示词展示"):
        assert label in html
    assert "模型配置" in html
    assert "管理员" in html
    assert admin0_client.get("/permissions").status_code == 200
    assert admin0_client.get("/prompts").status_code == 200
    assert admin0_client.get("/settings/llm").status_code == 200


def test_owner_can_write_1_to_5_but_not_admin_modules(owner_client):
    html = owner_client.get("/").text
    for label in ("①知识库", "②选题库", "③写作引擎", "④排期版", "⑤数据反馈"):
        assert label in html
    assert "⑥权限" not in html
    assert "⑦提示词展示" not in html
    assert "模型配置" not in html
    assert "定义者" in html
    assert owner_client.post("/brands", data={"name": "定义者品牌"}, follow_redirects=False).status_code == 303
    assert owner_client.get("/permissions").status_code == 403
    assert owner_client.get("/prompts").status_code == 403
    assert owner_client.get("/settings/llm").status_code == 403


def test_editor_reads_knowledge_and_writes_2_to_5(owner_client, editor_client):
    brand_id = owner_client.post("/brands", data={"name": "权限测试"}, follow_redirects=False).headers["location"].split("/")[-1]
    html = editor_client.get("/").text
    assert "选题者" in html
    assert "⑥权限" not in html and "⑦提示词展示" not in html and "模型配置" not in html
    assert editor_client.post(f"/brands/{brand_id}/define", data={"brand_prompt": "x"}, follow_redirects=False).status_code == 403
    assert editor_client.post("/topics/generate", data={"count": "1"}, follow_redirects=False).status_code in (303, 500)
    assert editor_client.get("/settings/llm").status_code == 403


def test_publisher_reads_1_to_3_and_writes_4_to_5(owner_client, publisher_client):
    html = publisher_client.get("/").text
    assert "发布者" in html
    assert "⑥权限" not in html and "⑦提示词展示" not in html and "模型配置" not in html
    assert publisher_client.get("/topics").status_code == 200
    assert publisher_client.get("/writing").status_code == 200
    assert publisher_client.post("/topics/generate", data={"count": "1"}, follow_redirects=False).status_code == 403
    assert publisher_client.post("/schedule/weeks/add", follow_redirects=False).status_code in (303, 400)
    assert publisher_client.get("/settings/llm").status_code == 403


def test_publisher_can_only_read_writing_engine(publisher_client, fresh_db):
    with Session(fresh_db) as session:
        brand = Brand(name="写作权限品牌")
        session.add(brand)
        session.commit()
        session.refresh(brand)
        campaign = Campaign(brand_id=brand.id, name="写作权限活动")
        session.add(campaign)
        session.commit()
        session.refresh(campaign)
        topic = Topic(brand_id=brand.id, campaign_id=campaign.id, title="发布者只读选题", status="采纳")
        session.add(topic)
        session.commit()
        session.refresh(topic)
        article = Article(
            topic_id=topic.id,
            campaign_id=campaign.id,
            title="发布者只读文章",
            body="正文",
            status="待审核",
        )
        session.add(article)
        session.commit()
        session.refresh(article)
        topic_id = topic.id
        article_id = article.id

    home = publisher_client.get("/writing")
    assert home.status_code == 200
    assert "发布者只读选题" in home.text
    assert "生成图文" not in home.text
    assert "新建" not in home.text

    detail = publisher_client.get(f"/writing/articles/{article_id}")
    assert detail.status_code == 200
    assert "发布者只读文章" in detail.text
    for forbidden_text in ("编辑正文", "审核通过", "审核未通过", "AI 审核", "上传替换"):
        assert forbidden_text not in detail.text

    assert publisher_client.post(f"/writing/topics/{topic_id}/generate", follow_redirects=False).status_code == 403
    assert publisher_client.post(f"/writing/articles/{article_id}/delete", follow_redirects=False).status_code == 403
    assert publisher_client.post(
        f"/writing/articles/{article_id}/review",
        data={"decision": "approve"},
        follow_redirects=False,
    ).status_code == 403
