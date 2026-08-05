"""⑧Git 协作 服务层——本地 Git 操作 + GitHub REST API 客户端。

所有 Git 命令通过 subprocess 安全执行（列表参数，不走 shell），
GitHub API 走 httpx（已在 requirements 中），token 从 config 读取。
无 token 时 PR 功能降级（git push 仍可用，依赖本机 git 凭据）。
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core import config


# ── Git 仓库路径 ──

def _repo_root() -> str:
    """返回 git 仓库根目录（动态探测，不硬编码路径）。"""
    r = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, timeout=5,
    )
    return r.stdout.strip() or "."


def _git(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """在仓库根目录执行 git 命令（列表参数防注入）。"""
    return subprocess.run(
        ["git"] + args,
        cwd=_repo_root(),
        capture_output=True, text=True, timeout=timeout,
    )


# ── 数据类 ──

@dataclass
class FileChange:
    path: str
    status: str          # staged 状态码（M/A/D/?? 等）
    work_status: str     # 工作区状态码
    staged: bool         # 是否已暂存


@dataclass
class CommitInfo:
    hash: str
    short_hash: str
    message: str
    author: str
    date: str


@dataclass
class BranchInfo:
    name: str
    current: bool
    remote_tracking: str  # 远程跟踪分支（如 origin/main），空则无


@dataclass
class PullRequest:
    number: int
    title: str
    state: str
    head: str              # 源分支
    base: str              # 目标分支
    html_url: str
    user: str
    created_at: str


@dataclass
class GitStatus:
    current_branch: str
    files: list[FileChange] = field(default_factory=list)
    commits: list[CommitInfo] = field(default_factory=list)
    ahead: int = 0         # 本地领先远程的提交数
    behind: int = 0        # 落后远程的提交数
    clean: bool = True     # 工作区是否干净
    error: str = ""


# ── 本地 Git 操作 ──

def get_status() -> GitStatus:
    """获取完整 git 状态：当前分支、变更文件、最近提交、ahead/behind。"""
    status = GitStatus(current_branch="")

    # 当前分支
    r = _git(["branch", "--show-current"])
    if r.returncode != 0:
        status.error = r.stderr.strip()
        return status
    status.current_branch = r.stdout.strip()

    # 变更文件（porcelain 格式）
    r = _git(["status", "--porcelain=v1"])
    if r.returncode == 0:
        for line in r.stdout.strip().splitlines():
            if not line:
                continue
            # 格式: XY path（X=暂存状态, Y=工作区状态）
            x = line[0] if len(line) > 0 else " "
            y = line[1] if len(line) > 1 else " "
            path = line[3:] if len(line) > 3 else line.strip()
            status.files.append(FileChange(
                path=path,
                status=x.strip(),
                work_status=y.strip(),
                staged=x not in (" ", "?"),
            ))
        status.clean = len(status.files) == 0

    # ahead/behind
    r = _git(["rev-list", "--left-right", "--count", "HEAD...@{upstream}"])
    if r.returncode == 0:
        parts = r.stdout.strip().split()
        if len(parts) == 2:
            status.ahead = int(parts[0])
            status.behind = int(parts[1])

    # 最近 10 条提交
    r = _git(["log", "--oneline", "-10", "--format=%H|%h|%s|%an|%ci"])
    if r.returncode == 0:
        for line in r.stdout.strip().splitlines():
            if not line:
                continue
            parts = line.split("|", 4)
            if len(parts) == 5:
                status.commits.append(CommitInfo(
                    hash=parts[0],
                    short_hash=parts[1],
                    message=parts[2],
                    author=parts[3],
                    date=parts[4],
                ))

    return status


def list_branches() -> list[BranchInfo]:
    """列出本地分支及远程跟踪信息。"""
    r = _git(["branch", "--format=%(refname:short)|%(objectname:short)|%(upstream:short)"])
    branches: list[BranchInfo] = []
    if r.returncode != 0:
        return branches

    # 获取当前分支
    cur = _git(["branch", "--show-current"]).stdout.strip()

    for line in r.stdout.strip().splitlines():
        if not line:
            continue
        parts = line.split("|")
        name = parts[0].replace("*", "").strip()
        tracking = parts[2].strip() if len(parts) > 2 else ""
        branches.append(BranchInfo(
            name=name,
            current=(name == cur),
            remote_tracking=tracking,
        ))
    return branches


def list_remote_branches() -> list[str]:
    """列出远程分支名（去掉 origin/ 前缀）。"""
    r = _git(["branch", "-r", "--format=%(refname:short)"])
    if r.returncode != 0:
        return []
    result = []
    for line in r.stdout.strip().splitlines():
        name = line.strip()
        # 跳过 HEAD -> origin/main 这类符号引用
        if "->" in name:
            continue
        if name.startswith("origin/"):
            name = name[len("origin/"):]
        if name and name not in result:
            result.append(name)
    return result


def stage_files(paths: list[str]) -> tuple[bool, str]:
    """暂存指定文件。返回 (成功, 消息)。"""
    if not paths:
        return False, "未选择文件"
    r = _git(["add"] + paths)
    if r.returncode != 0:
        return False, r.stderr.strip() or "暂存失败"
    return True, f"已暂存 {len(paths)} 个文件"


def stage_all() -> tuple[bool, str]:
    """暂存所有变更。"""
    r = _git(["add", "-A"])
    if r.returncode != 0:
        return False, r.stderr.strip() or "暂存失败"
    return True, "已暂存所有变更"


def commit(message: str) -> tuple[bool, str]:
    """提交暂存区。返回 (成功, 消息)。"""
    if not message.strip():
        return False, "提交信息不能为空"
    r = _git(["commit", "-m", message])
    if r.returncode != 0:
        return False, r.stderr.strip() or "提交失败"
    return True, r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "提交成功"


def push(remote: str = "origin", branch: str = "") -> tuple[bool, str]:
    """推送到远程。branch 为空时用当前分支。"""
    args = ["push", remote]
    if branch:
        args.append(branch)
    r = _git(args, timeout=60)
    if r.returncode != 0:
        return False, r.stderr.strip() or "推送失败"
    return True, "推送成功"


def create_branch(name: str, base: str = "") -> tuple[bool, str]:
    """从 base 创建并切换到新分支。base 为空时用当前分支。"""
    if not name.strip():
        return False, "分支名不能为空"
    if not re.match(r"^[\w./-]+$", name):
        return False, "分支名含非法字符"
    args = ["checkout", "-b", name]
    if base:
        args.append(base)
    r = _git(args)
    if r.returncode != 0:
        return False, r.stderr.strip() or "创建分支失败"
    return True, f"已创建并切换到分支 {name}"


def checkout_branch(name: str) -> tuple[bool, str]:
    """切换分支。"""
    if not re.match(r"^[\w./-]+$", name):
        return False, "分支名含非法字符"
    r = _git(["checkout", name])
    if r.returncode != 0:
        return False, r.stderr.strip() or "切换分支失败"
    return True, f"已切换到分支 {name}"


# ── GitHub 远程仓库解析 ──

def parse_remote_url() -> tuple[str, str]:
    """从 origin remote URL 解析 owner/repo。

    支持格式：
      - https://github.com/owner/repo.git
      - git@github.com:owner/repo.git
    返回 (owner, repo)，解析失败返回 ("", "")。
    """
    r = _git(["remote", "get-url", "origin"])
    if r.returncode != 0:
        return "", ""
    url = r.stdout.strip()

    # HTTPS: https://github.com/owner/repo.git
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?$", url)
    if m:
        return m.group(1), m.group(2)

    # SSH: git@github.com:owner/repo.git
    m = re.match(r"git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$", url)
    if m:
        return m.group(1), m.group(2)

    return "", ""


# ── GitHub REST API ──

_GITHUB_API = "https://api.github.com"


def _headers() -> dict[str, str]:
    """构建 GitHub API 请求头。无 token 时返回空 dict（API 会返回 401）。"""
    token = config.GITHUB_TOKEN.strip()
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def github_list_pulls(state: str = "open") -> tuple[list[PullRequest], str]:
    """列出 PR。返回 (PR列表, 错误消息)。"""
    owner, repo = parse_remote_url()
    if not owner:
        return [], "无法解析仓库地址（origin remote 未配置）"

    try:
        resp = httpx.get(
            f"{_GITHUB_API}/repos/{owner}/{repo}/pulls",
            params={"state": state, "per_page": 20},
            headers=_headers(),
            timeout=15,
        )
    except httpx.RequestError as e:
        return [], f"网络错误: {e}"

    if resp.status_code == 401:
        return [], "GitHub Token 无效或未配置（设置 TNALPHA_GITHUB_TOKEN 环境变量）"
    if resp.status_code != 200:
        return [], f"GitHub API 错误: {resp.status_code}"

    pulls: list[PullRequest] = []
    for item in resp.json():
        pulls.append(PullRequest(
            number=item["number"],
            title=item["title"],
            state=item["state"],
            head=item["head"]["ref"],
            base=item["base"]["ref"],
            html_url=item["html_url"],
            user=item.get("user", {}).get("login", ""),
            created_at=item.get("created_at", ""),
        ))
    return pulls, ""


def github_create_pull(
    title: str,
    body: str,
    head: str,
    base: str,
) -> tuple[PullRequest | None, str]:
    """创建 PR。返回 (PR对象, 错误消息)。"""
    owner, repo = parse_remote_url()
    if not owner:
        return None, "无法解析仓库地址（origin remote 未配置）"

    if not config.GITHUB_TOKEN.strip():
        return None, "未配置 GitHub Token（设置 TNALPHA_GITHUB_TOKEN 环境变量）"

    payload: dict[str, Any] = {
        "title": title,
        "body": body,
        "head": head,
        "base": base,
    }

    try:
        resp = httpx.post(
            f"{_GITHUB_API}/repos/{owner}/{repo}/pulls",
            json=payload,
            headers=_headers(),
            timeout=15,
        )
    except httpx.RequestError as e:
        return None, f"网络错误: {e}"

    if resp.status_code == 401:
        return None, "GitHub Token 无效"
    if resp.status_code == 422:
        return None, "PR 创建失败：分支相同或无差异（422）"
    if resp.status_code not in (200, 201):
        try:
            msg = resp.json().get("message", f"HTTP {resp.status_code}")
        except Exception:
            msg = f"HTTP {resp.status_code}"
        return None, f"GitHub API 错误: {msg}"

    item = resp.json()
    pr = PullRequest(
        number=item["number"],
        title=item["title"],
        state=item["state"],
        head=item["head"]["ref"],
        base=item["base"]["ref"],
        html_url=item["html_url"],
        user=item.get("user", {}).get("login", ""),
        created_at=item.get("created_at", ""),
    )
    return pr, ""


def github_list_branches_api() -> tuple[list[str], str]:
    """通过 GitHub API 列出远程分支（用于 PR 表单下拉）。"""
    owner, repo = parse_remote_url()
    if not owner:
        return [], "无法解析仓库地址"

    if not config.GITHUB_TOKEN.strip():
        # 回退到本地 git branch -r
        return list_remote_branches(), ""

    try:
        resp = httpx.get(
            f"{_GITHUB_API}/repos/{owner}/{repo}/branches",
            params={"per_page": 100},
            headers=_headers(),
            timeout=15,
        )
    except httpx.RequestError as e:
        return [], f"网络错误: {e}"

    if resp.status_code != 200:
        return list_remote_branches(), ""

    branches = [item["name"] for item in resp.json()]
    return branches, ""
