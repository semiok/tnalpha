"""模块入口 + 权限导航测试。"""
import pytest

def test_permissions_page_ok_for_admin0(admin0_client):
    path, name = "/permissions", "权限"
    r = admin0_client.get(path)
    assert r.status_code == 200
    assert name in r.text and "账号权限矩阵" in r.text


@pytest.mark.parametrize("path,_name", [("/permissions", "权限")])
def test_placeholder_requires_login(anon_client, path, _name):
    r = anon_client.get(path, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/login")


def test_writing_page_ok(owner_client):
    r = owner_client.get("/writing")
    assert r.status_code == 200
    assert "写作引擎" in r.text and "还没有品牌" in r.text


def test_writing_requires_login(anon_client):
    r = anon_client.get("/writing", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/login")


def test_schedule_page_ok(owner_client):
    r = owner_client.get("/schedule")
    assert r.status_code == 200
    assert "排期版" in r.text and "暂无可排期内容" in r.text


def test_schedule_requires_login(anon_client):
    r = anon_client.get("/schedule", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/login")


def test_feedback_page_ok(owner_client):
    r = owner_client.get("/feedback")
    assert r.status_code == 200
    assert "数据反馈" in r.text and "暂无发布数据" in r.text


def test_feedback_requires_login(anon_client):
    r = anon_client.get("/feedback", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/login")


def test_admin0_seven_module_nav(admin0_client):
    for page in ("/", "/topics", "/permissions", "/prompts"):
        html = admin0_client.get(page).text
        for label in ("①知识库", "②选题库", "③写作引擎", "④排期版", "⑤数据反馈", "⑥权限", "⑦提示词展示"):
            assert label in html


def test_owner_nav_hides_admin_modules(owner_client):
    html = owner_client.get("/").text
    for label in ("①知识库", "②选题库", "③写作引擎", "④排期版", "⑤数据反馈"):
        assert label in html
    assert "⑥权限" not in html
    assert "⑦提示词展示" not in html
    assert owner_client.get("/permissions").status_code == 403
    assert owner_client.get("/prompts").status_code == 403


def test_nav_hidden_when_anon(anon_client):
    # 未登录（登录页无导航）
    assert "②选题库" not in anon_client.get("/login").text
