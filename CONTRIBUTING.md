# CONTRIBUTING — 协作流程

> 配合 `CLAUDE.md`（AI 开发规则）+ `ARCHITECTURE.md`（技术框架）一起看。

## 角色

- **产品经理（你）**：认领一个 Linear issue（一个模块），用你的 AI 实现它，提 PR。
- **维护者**：review PR、合并、维护 `core/` 和地基文档。

## 一次性准备

1. 拿到仓库写权限（维护者把你加为 GitHub collaborator）。
2. clone：`git clone https://github.com/semiok/tnalpha`
3. 建环境：`python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt`
4. 让你的 AI 读 `CLAUDE.md`（它会告诉 AI 该读哪些、守哪些规矩）。

## 认领任务 → 开发 → 合并（标准闭环）

```
1. Linear 上认领一个 issue（如 MET-7 ②选题库），拖到 In Progress
2. 从 main 切分支：git checkout main && git pull && git checkout -b topic/xxx
3. 让 AI 照 CLAUDE.md + 你的 issue + knowledge 样板 开发（TDD）
4. 本地 pytest 全绿 0 warnings
5. 提交 → push → 开 PR，标题带 issue 号：[MET-7] ...
6. 维护者 review → 合并（squash）
7. 合并后 Linear issue 自动 → Done（GitHub 集成）
```

## 分支 & 提交

- 分支命名：`<模块>/<简述>`，如 `topic/recommend`、`writing/style-modal`。
- **PR 标题必须带 Linear issue 号** `[MET-N]`——这样 PR 自动关联 issue、合并自动流转状态。
- 提交信息用中文没问题，说清"做了什么"。
- 一个 PR 聚焦一件事，别把多个模块混在一个 PR。

## PR 前必须满足（否则不合并）

- [ ] `pytest -q` 全绿、**0 warnings**
- [ ] 只动了自己模块的目录（+ main.py/db.py 各一行注册）
- [ ] UI 用了 `components/ui.html` 组件，没自创样式
- [ ] 受控写操作有 `require_level` 守卫
- [ ] schema 改动带 Alembic 迁移
- [ ] PR 描述写清：做了什么 + 怎么测的

## 怎么新增一个模块（照样板）

1. `mkdir app/modules/<模块> && touch app/modules/<模块>/__init__.py`
2. 复制 `app/modules/knowledge/` 的 `models.py`/`routes.py` 结构改成你的
3. 模板放 `app/templates/<模块>/`，`{% extends "base.html" %}` + `{% from "components/ui.html" import ... %}`
4. `app/main.py` 加一行 `app.include_router(<模块>_routes.router)`
5. `app/core/db.py` 的 `init_db()` 里导入你的 models
6. 写测试放 `tests/`，跑绿

## 冲突最小化

- 每人一个模块目录 → 天然不冲突。
- 唯一可能撞的是 `main.py` / `db.py` 的注册行和 `requirements.txt`——保持**只加你那一行**，冲突也好解。
- 不要动别人的模块、`core/`、`DESIGN.md`、`components/ui.html`（要改先在 Linear 提）。

## 环境速查

| 事项 | 命令 |
|------|------|
| 跑服务 | `.venv/bin/uvicorn app.main:app --reload --port 8820` |
| 测试 | `.venv/bin/pytest -q` |
| 建迁移 | `.venv/bin/alembic revision --autogenerate -m "..."` |
| 应用迁移 | `.venv/bin/alembic upgrade head` |
| 登录账号 | admin / admin1 / admin2，密码都是 admin@123 |
