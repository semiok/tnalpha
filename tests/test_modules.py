"""模块入口 + 六模块导航测试。"""
import pytest

_PLACEHOLDERS = [
    # ②选题库(/topics)、③写作引擎(/writing)、④排期版(/schedule) 已实现，不再是占位
    ("/feedback", "数据反馈"),
    ("/permissions", "权限"),
]


@pytest.mark.parametrize("path,name", _PLACEHOLDERS)
def test_placeholder_page_ok(owner_client, path, name):
    r = owner_client.get(path)
    assert r.status_code == 200
    assert name in r.text and "开发中" in r.text


@pytest.mark.parametrize("path,_name", _PLACEHOLDERS)
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


def test_six_module_nav_on_pages(owner_client):
    # 登录后每页顶栏都有六模块导航
    for page in ("/", "/topics", "/permissions"):
        html = owner_client.get(page).text
        for label in ("①知识库", "②选题库", "③写作引擎", "④排期版", "⑤数据反馈", "⑥权限"):
            assert label in html


def test_nav_hidden_when_anon(anon_client):
    # 未登录（登录页无导航）
    assert "②选题库" not in anon_client.get("/login").text
