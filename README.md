# tnalpha

内容生产协作平台（tngen 单人验证版的协作 v2）。多个产品经理用 AI 各开一个模块，GitHub 协作合并。

## 六模块（闭环）

①知识库 → ②选题库 → ③写作引擎 → ④排期版 → ⑤数据反馈 →（反馈回流）→ ①
⑥权限管理：三角色（定义者 / 选题者 / 发布者）严格包含，横切所有模块。

## 当前进度

- [x] 模块拆解 + 逐模块线框图（ASCII 已确认）
- [x] 技术选型：模块化单体 · FastAPI + SQLModel + Postgres + HTMX/Alpine · 复用 M11 三角色 RBAC
- [x] 产品原型（高保真线框 + 模块标注）→ `docs/prototype.html`，在线：https://tnalpha.bplabs.xyz
- [ ] 总体技术框架文档（CTO 规范）— 待原型评审确认后
- [ ] 协作开发规范（AI 上手包）— 待原型评审确认后

## 原型

`docs/prototype.html` — 单文件，Tailwind + Alpine，含六模块关键页 + 每屏功能标注。
本地服务：launchd `ai.openclaw.tnalpha`（python http.server :8810，serve docs/）。
