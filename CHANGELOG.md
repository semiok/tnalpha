# 更新日志

本项目版本遵循 [语义化版本 SemVer](https://semver.org/lang/zh-CN/)。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [0.2.0] - 2026-07-03

### 新增
- **右上角「开发/演示」模式切换**：定义者一键切换全站模式——**开发模式**(动态知识库，能新建品牌/上传/AI解析) ⇄ **演示模式**(原型六模块只读演示壳)。状态存 DB(`AppSetting`)持久保存，重启/`--reload` 不丢。

### 变更
- **默认改为演示模式**（`KNOWLEDGE_WRITABLE` 默认 `false`）：clone 下来 / 未配 env 时先看只读演示壳，与线上部署一致——协作者不再困惑"本地是动态、线上是演示壳"。要开发点右上角切一下即可。env 只决定 DB 首次建行的初值，之后以 DB（页面切换）为准。

[0.2.0]: https://github.com/semiok/tnalpha/releases/tag/v0.2.0

## [0.1.0] - 2026-07-03

首个里程碑：能登录、能演示六模块全貌、能配真实模型。

### 新增
- **登录 + 三角色 RBAC**（定义者 / 选题者 / 发布者；hmac cookie；服务端 `require_level` 守卫 + 模板按 level 显隐）
- **①知识库**样板模块：品牌 / Campaign / 文档上传 / AI 解析 / 全局数据池（CRUD + 文档抽取 + Alembic 迁移）
- **core 抽象层**：`llm` / `sources` / `storage` / `docparse`（stub 先行，模块不直连外部 API）
- **模型配置（多 provider 路由）**：定义者顶栏「模型配置」`/settings/llm` → 其他 API(OpenAI 兼容) / Claude 授权 / Codex 授权；DB 驱动即时切换、无需重启；任何失败回退 stub；对外签名不变
- **只读演示壳**：登录后即原型六模块全貌（`KNOWLEDGE_WRITABLE=false` 时），后端 CRUD 代码保留、翻开关恢复动态
- **组件库** `components/ui.html`（统一按钮/卡片/表单/横幅宏）+ base 设计 token
- 全站顶栏显示**版本号**

### 基础设施
- **dev / prod 双环境**：`tnalpha.bplabs.xyz`(dev, --reload) / `tnapp.bplabs.xyz`(prod)，独立代码副本 + 独立数据库
- 质量红线：`pytest` 全绿 + `filterwarnings=error`（0 warnings 强制）

[0.1.0]: https://github.com/semiok/tnalpha/releases/tag/v0.1.0
