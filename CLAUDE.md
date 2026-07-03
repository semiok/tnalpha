# CLAUDE.md — tnalpha 开发规则（AI 必读）

> **每个贡献者的 AI（Claude / GPT / Gemini 皆可）在动手前必须读完这份。** 规则不在模型里，在这个文件里——照它做，不同人的代码才能合到一起。

## 0. 你在做什么

tnalpha 是内容生产协作平台。多人各用 AI 负责一个模块，GitHub 协作合并。你被分配了一个模块（见你的 Linear issue），任务是**在既定框架内**实现它。

> ⚡ **每次开工第一件事**：先读 Linear「📌 协作留言板」(**MET-17**，https://linear.app/metasnowsky/issue/MET-17) 的最新留言——维护者的最新同步、你依赖的东西好没好（如 ②的 `Topic` 进 main 没）、临时约定都在那儿。看完再动手，避免各做各的、撞车。有卡点 / 要改 core / PR 开了，也回这里留言。

先读这几样，再写第一行代码：
1. 本文件（规则）
2. `CONTRIBUTING.md`（**协作流程**：怎么开发 / 本地验证 / 提 PR，贡献者做什么、维护者做什么）
3. `ARCHITECTURE.md`（技术框架 + 目录结构 + 模块间接口契约）
4. `DESIGN.md`（UI/交互规范）
5. `需求文档.md`（对应你模块的章节）
6. **`app/modules/knowledge/`（参考样板——照它抄写法）**
7. 你的 Linear issue 描述（模块要做什么 + 验收标准）

## 1. 铁律（违反 = PR 不合并）

1. **只改自己模块的目录** `app/modules/<你的模块>/` + `app/templates/<你的模块>/`。**不要动 `app/core/`、别人的模块、`DESIGN.md`、`components/ui.html`**。要改 core，先在 Linear 提出来。
2. **UI 只用 `app/templates/components/ui.html` 的组件宏**，不自创按钮/卡片/配色。配色用 `brand` token（见 DESIGN.md）。
3. **搜索/AI 调用走 `core/` 抽象层**：搜索源用 `core/sources/`，LLM 用 `core/llm/`。**不要在模块里直接 curl 外部 API**。真实源没接好就用 stub。
4. **权限**：受控写操作用 `auth.require_level(request, n)` 守卫（服务端）+ 模板 `request.state.level >= n` 显隐（UI）。level 见你的 issue。
5. **TDD**：先写测试再写实现。`pytest` 必须全过、**0 warnings**（`filterwarnings=error` 已配）。
6. **提交规范**：分支从 `main` 切，命名 `<模块>/<简述>`；PR 标题带 Linear issue 号（如 `[MET-7] ...`）。

## 2. 目录约定（每个模块长一样）

```
app/modules/<模块>/
├─ __init__.py
├─ models.py       # SQLModel 表（只定义本模块的表）
├─ routes.py       # FastAPI router（本模块所有路由）
├─ services.py     # 业务逻辑（可选，复杂时拆出来）
└─ （模板放 app/templates/<模块>/）
```
新模块要在 `app/main.py` 注册 router（一行 `include_router`），并在 `app/core/db.py` 的 `init_db` 导入 models。改这两处 core 是允许的例外（只加你模块那一行）。

## 3. 开发流程

```bash
# 1. 环境（一次）
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2. 跑起来（SQLite 开发库，零配置）
.venv/bin/uvicorn app.main:app --reload --port 8820
# 浏览 http://127.0.0.1:8820  账号 admin/admin@123（定义者）

# 3. 测试（必须全绿 0 warnings）
.venv/bin/pytest -q

# 4. 数据库 schema 变了 → 建迁移
.venv/bin/alembic revision --autogenerate -m "add xxx"
```

## 4. 写代码前的自检清单

- [ ] 我只在 `app/modules/<我的模块>/` 和 `app/templates/<我的模块>/` 里改动？
- [ ] UI 全部用了 `components/ui.html` 的宏？没自创样式？
- [ ] 外部搜索/LLM 走了 `core/sources/` `core/llm/`（或 stub）？
- [ ] 受控写操作加了 `require_level` 守卫？
- [ ] 有测试、`pytest` 全绿 0 warnings？
- [ ] schema 改动有 Alembic 迁移？
- [ ] 跟我模块相关的**跨模块契约**（如数据池共享表）对照了 `ARCHITECTURE.md`？

## 5. 遇到问题

- 需要改 core / 别人的模块 / 加新组件 → **在 Linear issue 里提出来**，不要擅自改。
- 跨模块的数据/接口不确定 → 查 `ARCHITECTURE.md` 的「模块间契约」，还不清楚就在 issue 里 @ 相关人。
- **不确定就问，别自由发挥**——自由发挥是合不拢的头号原因。

---
*样板即答案：`app/modules/knowledge/` 是照抄的标准范例。你的模块结构、命名、测试写法都应与它一致。*
