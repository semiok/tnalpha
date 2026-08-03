"""OpenLoomi agent API authentication, reads, writes and audit coverage."""
from sqlmodel import Session, select

from app.core import config
from app.modules.agent_api.models import AgentAuditLog
from app.modules.knowledge.models import Brand, Campaign
from app.modules.topic.models import Topic


TOKEN = "test-openloomi-agent-token"


def _headers(**extra):
    return {
        "Authorization": f"Bearer {TOKEN}",
        "X-TNAlpha-Actor": "openloomi-test-admin",
        "X-TNAlpha-Org": "tnalpha-test",
        **extra,
    }


def test_agent_api_requires_bearer_token(anon_client, monkeypatch):
    monkeypatch.setattr(config, "AGENT_API_TOKEN", TOKEN)
    response = anon_client.get("/api/v1/me")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")


def test_agent_api_reports_admin_identity_and_capabilities(anon_client, monkeypatch):
    monkeypatch.setattr(config, "AGENT_API_TOKEN", TOKEN)
    me = anon_client.get("/api/v1/me", headers=_headers())
    assert me.status_code == 200
    assert me.json() == {
        "actor_id": "openloomi-test-admin",
        "org_id": "tnalpha-test",
        "role": "admin0",
        "role_label": "管理员",
        "access": "highest",
    }
    capabilities = anon_client.get("/api/v1/capabilities", headers=_headers()).json()
    assert len(capabilities["modules"]) == 7
    assert "topics.create_manual" in capabilities["implemented_actions"]
    assert "publish.execute" in capabilities["restricted_actions"]


def test_agent_api_reads_brand_overview(anon_client, fresh_db, monkeypatch):
    monkeypatch.setattr(config, "AGENT_API_TOKEN", TOKEN)
    with Session(fresh_db) as session:
        brand = Brand(name="溯肤", analysis_status="done")
        session.add(brand)
        session.commit()
        session.refresh(brand)
        session.add(Campaign(brand_id=brand.id, name="品牌常青", is_default=True))
        session.add(Topic(brand_id=brand.id, title="测试选题", status="候选"))
        session.commit()
        brand_id = brand.id

    brands = anon_client.get("/api/v1/brands", headers=_headers()).json()
    assert brands["count"] == 1
    assert brands["items"][0]["name"] == "溯肤"
    overview = anon_client.get(f"/api/v1/brands/{brand_id}/overview", headers=_headers()).json()
    assert overview["campaigns"] == 1
    assert overview["topics"] == {"total": 1, "by_status": {"候选": 1}}


def test_agent_api_creates_exact_manual_topic_and_audit(anon_client, fresh_db, monkeypatch):
    monkeypatch.setattr(config, "AGENT_API_TOKEN", TOKEN)
    with Session(fresh_db) as session:
        brand = Brand(name="溯肤")
        session.add(brand)
        session.commit()
        session.refresh(brand)
        campaign = Campaign(brand_id=brand.id, name="新品发布")
        session.add(campaign)
        session.commit()
        session.refresh(campaign)
        brand_id, campaign_id = brand.id, campaign.id

    title = "OpenLoomi 自然语言新增的测试选题"
    response = anon_client.post(
        "/api/v1/topics/manual",
        headers=_headers(**{"X-Request-ID": "loomi-test-001"}),
        json={
            "brand_id": brand_id,
            "campaign_id": campaign_id,
            "title": title,
            "outline": "验证数字员工能在不修改标题的前提下创建候选选题。",
        },
    )
    assert response.status_code == 201
    assert response.json()["item"]["title"] == title
    assert response.json()["item"]["status"] == "候选"

    with Session(fresh_db) as session:
        topic = session.exec(select(Topic).where(Topic.title == title)).one()
        audit = session.exec(select(AgentAuditLog)).one()
        assert topic.source == "added"
        assert audit.request_id == "loomi-test-001"
        assert audit.actor_id == "openloomi-test-admin"
        assert audit.action == "topics.create_manual"
        assert audit.resource_id == str(topic.id)

    duplicate = anon_client.post(
        "/api/v1/topics/manual",
        headers=_headers(),
        json={"brand_id": brand_id, "campaign_id": campaign_id, "title": title},
    )
    assert duplicate.status_code == 409
