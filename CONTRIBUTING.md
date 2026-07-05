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

## 模型配置：接入你的 AI（本地开发）

开发时**默认走 `stub`**（假数据、零配置，端到端能跑），不用接真实模型也能开发。要看真实 AI 解析/生成，去顶栏 **「模型配置」`/settings/llm`** 选 provider。**改完即时生效、失败自动回退 stub。**

文本 provider 5 选 1，**本机有哪个用哪个**：

| Provider | 怎么接 | 要不要 API Key |
|---|---|---|
| `stub` | 默认，什么都不用做 | 否 |
| `openai` | 填 Base URL / Model / 你的 Key（兼容 OpenAI 的都行：DeepSeek/Moonshot…） | **要** |
| `minimax-m3` | 选它自动填 MiniMax 预设，填你的 MiniMax Key | **要** |
| `claude-cli` | **授权模式**：本机装 Claude CLI 并登录过 | **不要** |
| `codex` | **授权模式**：本机装 codex 并登录过 | **不要** |

### 授权模式（claude-cli / codex）怎么连——零 Key

原理：**app 直接读你本机 CLI 登录后生成的 token 文件**，不用在 app 里填 Key、也没有"授权按钮"。两步：

1. **本机登录一次**（系统操作，弹浏览器授权，不在 app 里做）：
   - codex：`codex login` → 生成 `~/.codex/auth.json`
   - claude：本机 `claude` 登录（Max/Pro 订阅）
2. **模型配置页**：Provider 选 `codex`（或 `claude-cli`）→ 徽章会显示「已检测到 Codex / claude 授权」→ 保存即连上。

> ⚠️ **前提坑**：app 读的是**「运行 app 那台机器」**的 token 文件。你**本地跑 dev**（`uvicorn ... --port 8820` 在你自己机器）→ 读你本机的登录 → 没问题。别指望在别人的服务器上用你的授权。
>
> 未检测到？说明本机没登录——先 `codex login` / `claude` 登录，再刷新配置页。

### 按模块配置（预留接口）

模型可按模块配（`scope`）：默认锚点 `default`（=知识库那套），**未单配的模块自动继承 default**。你的模块要用别的模型：调用处传 `module="你的模块名"` + 存一行 `LLMSetting(scope=...)`（详见 `app/core/settings.py` 顶部注释 / `ARCHITECTURE.md §3`）。没配就继承默认，不影响开发。

### 热点搜索源（`core/sources/`）的 key——自己配，零外部依赖

②选题库的联网搜索源**全部自包含在 tnalpha 内**（`core/sources/`，urllib/bs4），**不依赖任何本机脚本或 OpenClaw**。要真联网，在你自己的 env 配 key（没配 → 该源 UI 灰掉「未启用」，其他源照跑、不报错）：

| 源 | env | 说明 |
|----|-----|------|
| Google（`google`） | `TNALPHA_GEMINI_API_KEY` | Gemini grounding，免费额度低（~200/天，[AI Studio](https://aistudio.google.com/apikey) 申自己的一把，别共用） |
| 搜狗公众号（`mp`） | 无需 key | 抓取内联，装 `beautifulsoup4`（在 requirements）即可 |
| 🔥深度热点（`sonar`） | `TNALPHA_PERPLEXITY_API_KEY` | Perplexity，付费 $1/千次 |
| 小红书（`xhs`） | — | 占位未开发 |

## 标准协作流程（谁做什么，务必分清）

**你（贡献者/模块负责人）做 1–5；维护者做 6–8。你把 PR 开好（第 5 步）活就完了，剩下等 review。**

```
── 贡献者（你 + 你的 AI）──
1. Linear 认领 issue（如 MET-7），拖到 In Progress
2. 从 main 切分支：git checkout main && git pull && git checkout -b topic/xxx
3. 照 CLAUDE.md + 你的 issue + knowledge 样板 开发（TDD，先写测试）
4. 本地验证（两样都要，缺一不可）：
     · .venv/bin/pytest -q                          → 全绿、0 warnings
     · .venv/bin/uvicorn app.main:app --port 8820   → 浏览器点自己模块，确认真能用
5. 提交 → push 分支 → 开 PR，标题带 [MET-N]，描述写清「做了什么 + 怎么测的」
── 维护者 ──
6. Review PR（可派 code-review agent 审契约/安全/质量）——别人 AI 的代码不盲合
7. 审过 → 合并 main（squash）→ Linear issue 自动 Done
8. 部署最新 main 到验证环境，人工点检
```

**三条铁律**：
- **不直接推 main**，一律走分支 + PR。
- **本地不只跑 pytest，还要起服务点一遍**——测试绿 ≠ 界面能用。
- **review 关不能省**：多 AI 协作，代码合进 main 前必须有人（或 review agent）把关契约与质量。MET-6 的 review 就抓出过会被照抄传播的缺陷。

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
