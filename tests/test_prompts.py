def test_prompts_page_lists_core_prompt_sources(owner_client):
    r = owner_client.get("/prompts")
    assert r.status_code == 200
    text = r.text
    for phrase in [
        "提示词展示",
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


def test_prompts_preview_mode_shows_current_preview(owner_client):
    r = owner_client.get("/prompts?mode=preview")
    assert r.status_code == 200
    assert "当前预览" in r.text
    assert "不代表写死" in r.text


def test_nav_contains_prompts_module(owner_client):
    r = owner_client.get("/")
    assert r.status_code == 200
    assert "⑦提示词展示" in r.text
