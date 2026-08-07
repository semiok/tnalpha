from types import SimpleNamespace

from sqlmodel import Session, select

from app.core.prompt_override import PromptOverride, load_cache, resolve


def test_prompts_page_lists_core_prompt_sources(admin0_client):
    r = admin0_client.get("/prompts")
    assert r.status_code == 200
    text = r.text
    for phrase in [
        "提示词管理",
        "①知识库",
        "②选题库",
        "③写作引擎",
        "④排期版",
        "⑤数据反馈",
        "生成候选选题",
        "生成图文",
        "AI 推荐排期",
        "经验生成",
    ]:
        assert phrase in text
    assert "通用模板" in text
    assert "{campaign_digest}" in text
    assert "Campaign 总体经验包引用策略" in text
    assert "当前 campaign 70%" in text


def test_prompts_preview_mode_shows_current_preview(admin0_client):
    r = admin0_client.get("/prompts?mode=preview")
    assert r.status_code == 200
    assert "当前预览" in r.text
    assert "如需编辑提示词" in r.text


def test_nav_contains_prompts_module(admin0_client):
    r = admin0_client.get("/")
    assert r.status_code == 200
    assert "⑦提示词管理" in r.text


def test_admin_can_save_and_reset_prompt_override(admin0_client, fresh_db):
    with Session(fresh_db) as session:
        load_cache(session)

    template = "文件：{filename}\n保留：{unknown}\n属性：{obj.missing}"
    saved = admin0_client.post(
        "/prompts/knowledge:style_analysis/save",
        data={"template": template},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert resolve(
        "knowledge:style_analysis",
        "默认 {filename}",
        filename="example.pdf",
        obj=SimpleNamespace(),
    ) == "文件：example.pdf\n保留：{unknown}\n属性：{obj.missing}"
    with Session(fresh_db) as session:
        row = session.exec(
            select(PromptOverride).where(
                PromptOverride.key == "knowledge:style_analysis"
            )
        ).one()
        assert row.template == template

    reset = admin0_client.post(
        "/prompts/knowledge:style_analysis/reset",
        follow_redirects=False,
    )
    assert reset.status_code == 303
    assert resolve(
        "knowledge:style_analysis", "默认 {filename}", filename="example.pdf"
    ) == "默认 example.pdf"


def test_non_admin_cannot_change_prompt_override(owner_client):
    response = owner_client.post(
        "/prompts/knowledge:style_analysis/save",
        data={"template": "不应保存"},
        follow_redirects=False,
    )
    assert response.status_code == 403
