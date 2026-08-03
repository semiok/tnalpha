# tnalpha

内容生产协作平台（tngen 单人验证版的协作 v2）。多个产品经理用 AI 各开一个模块，GitHub 协作合并。

## 六模块（闭环）

①知识库 → ②选题库 → ③写作引擎 → ④排期版 → ⑤数据反馈 →（反馈回流）→ ①
⑥权限管理：三角色（定义者 / 选题者 / 发布者）严格包含，横切所有模块。

## 📄 开发需求文档

**[需求文档.md](需求文档.md)** ← 完整需求（产品/六模块/数据模型/交互/技术选型/闭环）。用 AI 读这份即可理解全系统、画原型、分模块开发。

配套高保真原型：[docs/prototype.html](docs/prototype.html) · 在线 https://tnalpha.bplabs.xyz

## 当前进度

- [x] 模块拆解 + 逐模块线框图（ASCII 已确认）
- [x] 技术选型：模块化单体 · FastAPI + SQLModel + Postgres + HTMX/Alpine · 复用 M11 三角色 RBAC
- [x] 产品原型（高保真线框 + 模块标注）→ `docs/prototype.html`，在线：https://tnalpha.bplabs.xyz
- [ ] 总体技术框架文档（CTO 规范）— 待原型评审确认后
- [ ] 协作开发规范（AI 上手包）— 待原型评审确认后

## 配置真实模型（AI 解析要看到真效果）

默认所有 AI 调用走 **stub**（确定性假数据，零依赖，`git clone` 即跑，本地开发够用）。
想看真效果，**登录为定义者 → 顶栏「模型配置」**（`/settings/llm`），三选一，**改完立即生效、无需重启**：

| 模式 | 用什么 | 你需要 |
|------|--------|--------|
| **其他 API**（OpenAI 兼容） | OpenAI / DeepSeek / Moonshot / MiniMax / Ollama… | Base URL + Model + API Key |
| **Claude 授权** | 本机 `claude` CLI（Max 订阅，零 API 费） | 装好 claude CLI 并已登录 |
| **Codex 授权** | 本机 `~/.codex/auth.json`（出图） | 已登录 Codex |

配置存本机 SQLite（开发默认 `tnalpha.db`、本项目部署用 `data/app.db`，均已 gitignore，**不进公开仓**），API Key 页面打码显示。
兄弟本地没 Claude？填自己的 LLM API Key 走「其他 API」即可，一样能跑真解析。

生产环境同样支持——也可用 `.env`（见 `.env.example`）设初值，DB 设置优先。任何 provider 出错自动回退 stub，端到端不崩。

## 原型

`docs/prototype.html` — 单文件，Tailwind + Alpine，含六模块关键页 + 每屏功能标注。
本地服务：launchd `ai.openclaw.tnalpha`（python http.server :8810，serve docs/）。

## Agent Skill

TN-Alpha 对外 Agent 能力的唯一源代码位于
[`skills/tnalpha-content-ops`](skills/tnalpha-content-ops)。OpenRice、Codex、Claude
及其他兼容 Agent Skills 的平台应从 TN-Alpha 安装，不维护各自的副本。

- 生产安装清单：`https://alpha.traditionow.ai/skills/tnalpha-content-ops/manifest.json`
- Skill 注册表：`https://alpha.traditionow.ai/skills/registry.json`
- 开发源：本仓库 `main` 分支的 `skills/tnalpha-content-ops`

安装器读取清单后下载版本化 ZIP，并校验其中的 SHA-256。首次使用时在本机执行：

```bash
python3 scripts/tnalpha_client.py configure \
  --url https://alpha.traditionow.ai \
  --account YOUR_ACCOUNT \
  --org YOUR_ORG
python3 scripts/tnalpha_client.py status
```

非敏感连接信息写入 `~/.config/tnalpha/agent.json`；macOS 凭据进入 Keychain，
CI 或其他平台通过 `TNALPHA_AGENT_API_TOKEN` 注入。Skill 和仓库均不保存凭据。
