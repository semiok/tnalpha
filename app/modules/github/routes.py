"""⑧Git 协作 路由——推送代码 + 创建 PR。

权限：写操作（commit/push/branch/pr）= 定义者(owner, level 2)及以上；
浏览（查看状态/PR 列表）= 所有登录角色。
"""
from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.requests import Request

from app.core import auth, config
from app.core.templates import create_templates
from app.modules.github import services

router = APIRouter()
templates = create_templates()


def _hx_redirect(request: Request, url: str) -> Response:
    """HTMX 请求返回 HX-Redirect 头，普通请求返回 303。"""
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


@router.get("/github", response_class=HTMLResponse)
def github_home(request: Request):
    """主页面：Git 状态 + 分支 + PR 列表 + 操作表单。"""
    status = services.get_status()
    branches = services.list_branches()
    pulls, pr_error = services.github_list_pulls()
    api_branches, br_error = services.github_list_branches_api()
    owner, repo = services.parse_remote_url()
    token_configured = bool(config.GITHUB_TOKEN.strip())

    return templates.TemplateResponse(request, "github/home.html", {
        "status": status,
        "branches": branches,
        "pulls": pulls,
        "pr_error": pr_error,
        "api_branches": api_branches,
        "br_error": br_error,
        "repo_owner": owner,
        "repo_name": repo,
        "token_configured": token_configured,
    })


@router.post("/github/stage")
def github_stage(
    request: Request,
    files: list[str] = Form(default=[]),
    stage_all: str = Form(default=""),
):
    """暂存文件。"""
    auth.require_level(request, 2)
    if stage_all:
        ok, msg = services.stage_all()
    else:
        ok, msg = services.stage_files(files)
    return _hx_redirect(request, "/github")


@router.post("/github/commit")
def github_commit(
    request: Request,
    message: str = Form(...),
):
    """提交暂存区。"""
    auth.require_level(request, 2)
    ok, msg = services.commit(message)
    return _hx_redirect(request, "/github")


@router.post("/github/push")
def github_push(
    request: Request,
    branch: str = Form(default=""),
):
    """推送到远程。"""
    auth.require_level(request, 2)
    ok, msg = services.push(branch=branch)
    return _hx_redirect(request, "/github")


@router.post("/github/commit-push")
def github_commit_and_push(
    request: Request,
    message: str = Form(...),
    files: list[str] = Form(default=[]),
    stage_all: str = Form(default=""),
    branch: str = Form(default=""),
):
    """一步完成：暂存 → 提交 → 推送。"""
    auth.require_level(request, 2)

    # 1. 暂存
    if stage_all:
        ok, msg = services.stage_all()
    else:
        ok, msg = services.stage_files(files)
    if not ok:
        return _hx_redirect(request, "/github")

    # 2. 提交
    ok, msg = services.commit(message)
    if not ok:
        return _hx_redirect(request, "/github")

    # 3. 推送
    ok, msg = services.push(branch=branch)
    return _hx_redirect(request, "/github")


@router.post("/github/branch")
def github_create_branch(
    request: Request,
    name: str = Form(...),
    base: str = Form(default=""),
):
    """创建新分支。"""
    auth.require_level(request, 2)
    ok, msg = services.create_branch(name, base)
    return _hx_redirect(request, "/github")


@router.post("/github/checkout")
def github_checkout(
    request: Request,
    name: str = Form(...),
):
    """切换分支。"""
    auth.require_level(request, 2)
    ok, msg = services.checkout_branch(name)
    return _hx_redirect(request, "/github")


@router.post("/github/pr")
def github_create_pr(
    request: Request,
    title: str = Form(...),
    body: str = Form(default=""),
    head: str = Form(...),
    base: str = Form(...),
):
    """创建 Pull Request。"""
    auth.require_level(request, 2)
    pr, error = services.github_create_pull(title=title, body=body, head=head, base=base)
    return _hx_redirect(request, "/github")


@router.get("/github/pulls")
def github_pulls_fragment(request: Request):
    """HTMX 片段：刷新 PR 列表。"""
    pulls, error = services.github_list_pulls()
    return templates.TemplateResponse(request, "github/_pulls.html", {
        "pulls": pulls,
        "pr_error": error,
    })
