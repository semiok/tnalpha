"""⑧Git 协作模块测试：页面访问 + 权限守卫 + 服务层逻辑。"""
import re
from unittest.mock import patch, MagicMock

from app.modules.github import services


# ── 页面访问与权限 ──

def test_anon_cannot_access_github(anon_client):
    """未登录访问 /github 跳转登录。"""
    r = anon_client.get("/github", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_admin0_can_access_github(admin0_client):
    """管理员可见 Git 协作模块。"""
    r = admin0_client.get("/github")
    assert r.status_code == 200
    assert "Git 协作" in r.text
    assert "⑧" in r.text


def test_owner_can_access_github(owner_client):
    """定义者可见且有写权限。"""
    r = owner_client.get("/github")
    assert r.status_code == 200
    assert "推送" in r.text
    assert "创建 PR" in r.text or "Pull Request" in r.text


def test_editor_cannot_access_github(editor_client):
    """选题者不可见 Git 协作（仅 owner+ 可见）。"""
    r = editor_client.get("/github")
    assert r.status_code == 403


def test_publisher_cannot_access_github(publisher_client):
    """发布者不可见 Git 协作。"""
    r = publisher_client.get("/github")
    assert r.status_code == 403


def test_nav_includes_github_for_admin0(admin0_client):
    """管理员导航栏包含 ⑧Git 协作。"""
    html = admin0_client.get("/").text
    assert "⑧Git 协作" in html


def test_nav_includes_github_for_owner(owner_client):
    """定义者导航栏包含 ⑧Git 协作。"""
    html = owner_client.get("/").text
    assert "⑧Git 协作" in html


def test_nav_excludes_github_for_editor(editor_client):
    """选题者导航栏不含 ⑧Git 协作。"""
    html = editor_client.get("/").text
    assert "⑧Git 协作" not in html


# ── 写操作权限守卫 ──

def test_editor_cannot_commit(editor_client):
    """选题者不能提交代码。"""
    r = editor_client.post("/github/commit", data={"message": "test"}, follow_redirects=False)
    assert r.status_code == 403


def test_editor_cannot_push(editor_client):
    """选题者不能推送代码。"""
    r = editor_client.post("/github/push", follow_redirects=False)
    assert r.status_code == 403


def test_editor_cannot_create_pr(editor_client):
    """选题者不能创建 PR。"""
    r = editor_client.post("/github/pr", data={
        "title": "test", "head": "feature", "base": "main",
    }, follow_redirects=False)
    assert r.status_code == 403


def test_editor_cannot_create_branch(editor_client):
    """选题者不能创建分支。"""
    r = editor_client.post("/github/branch", data={"name": "test"}, follow_redirects=False)
    assert r.status_code == 403


# ── 服务层：parse_remote_url ──

def test_parse_remote_url_https():
    """HTTPS 格式 URL 解析。"""
    with patch.object(services, "_git") as mock_git:
        mock_git.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/owner/repo.git\n",
        )
        owner, repo = services.parse_remote_url()
        assert owner == "owner"
        assert repo == "repo"


def test_parse_remote_url_ssh():
    """SSH 格式 URL 解析。"""
    with patch.object(services, "_git") as mock_git:
        mock_git.return_value = MagicMock(
            returncode=0,
            stdout="git@github.com:semiok/tnalpha.git\n",
        )
        owner, repo = services.parse_remote_url()
        assert owner == "semiok"
        assert repo == "tnalpha"


def test_parse_remote_url_no_remote():
    """无 remote 时返回空字符串。"""
    with patch.object(services, "_git") as mock_git:
        mock_git.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        owner, repo = services.parse_remote_url()
        assert owner == ""
        assert repo == ""


# ── 服务层：commit 输入验证 ──

def test_commit_empty_message():
    """空提交信息被拒绝。"""
    with patch.object(services, "_git") as mock_git:
        ok, msg = services.commit("")
        assert not ok
        assert "不能为空" in msg
        mock_git.assert_not_called()


def test_create_branch_invalid_name():
    """非法分支名被拒绝。"""
    with patch.object(services, "_git") as mock_git:
        ok, msg = services.create_branch("bad;name")
        assert not ok
        assert "非法" in msg
        mock_git.assert_not_called()


def test_create_branch_valid_name():
    """合法分支名通过验证。"""
    with patch.object(services, "_git") as mock_git:
        mock_git.return_value = MagicMock(returncode=0, stdout="", stderr="")
        ok, msg = services.create_branch("feature/test-123")
        assert ok


def test_stage_files_empty():
    """空文件列表被拒绝。"""
    ok, msg = services.stage_files([])
    assert not ok
    assert "未选择" in msg


# ── 服务层：GitHub API（mock）──

def test_github_create_pull_no_token():
    """无 Token 时创建 PR 返回错误。"""
    with patch.object(services.config, "GITHUB_TOKEN", ""), \
         patch.object(services, "parse_remote_url", return_value=("owner", "repo")):
        pr, error = services.github_create_pull("t", "b", "head", "main")
        assert pr is None
        assert "Token" in error


def test_github_list_pulls_no_remote():
    """无 remote 时列出 PR 返回错误。"""
    with patch.object(services, "parse_remote_url", return_value=("", "")):
        pulls, error = services.github_list_pulls()
        assert pulls == []
        assert "无法解析" in error
