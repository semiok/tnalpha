# 更新日志

本项目版本遵循 [语义化版本 SemVer](https://semver.org/lang/zh-CN/)。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [0.6.1] - 2026-07-04

### 修复
- **活动 AI 解析现在真读图片/PDF**：修复「引用的图片型数据池」与「深度读图 PDF」未被 vision 读取——此前活动解析只把数据池 `content` 文本拼进去，图片无正文 → 完全丢失（用户反馈"图片未附上"）；深度读图 PDF 也只喂了抽取文字（网页转 PDF 常抽到链接/导航垃圾）。现 `core/llm` 加 `attachments` 参数（图片→`input_image`、PDF→`input_file`，支持多附件，codex/claude 通用），`run_campaign_analysis` 把 deep_read 文档 + 图片型数据池收集为附件、一次性交给 vision 读。实测 codex 准确读出 PDF 正文（展期/文物尺寸/作品名）+ 竹简图片内容。

[0.6.1]: https://github.com/semiok/tnalpha/releases/tag/v0.6.1

## [0.6.0] - 2026-07-04

### 新增
- **活动（campaign）资料支持深度读图**（与品牌管理一致）：每份活动资料可勾「深度读图」，AI 解析改**异步**（后台线程 + 状态轮询，同 brand）——读 PDF 图片页 + 品牌定义 + 引用数据池 → 生成活动提示词（供②选题库读）。
- **数据池支持资料上传**：新增资料包可上传文件，自动抽正文入 `content`（手填正文优先），存原文件供下载（`pooltopic.file_path`，追加列，②⑤ 只读 content 不受影响）。
- **Codex 文本 provider 自动重试**：偶发 OpenAI 瞬时错误（`response.failed` / 5xx / 空响应）自动重试（首次 + 2 次，递增退避）；授权错误（401/403）不重试直接抛。

### 变更
- **去除默认 campaign「品牌日常」**：品牌库已承载品牌相关内容，不再自动建常驻 campaign（`_default_brand` / `create_brand` 不再建，删除守卫）。
- **首页 campaign 列表行加高**（两行布局：名称 + 时效/已解析状态）。

[0.6.0]: https://github.com/semiok/tnalpha/releases/tag/v0.6.0

## [0.5.1] - 2026-07-04

### 变更
- **Codex 文本默认思考档位 high → medium**（速度/质量平衡；`TNALPHA_CODEX_REASONING` 可覆盖 high/low）。徽章文案同步为「gpt-5.5·思考medium」。注：全流程主要耗时在深度读图（PDF）调用，思考档位对文本调用影响有限。

[0.5.1]: https://github.com/semiok/tnalpha/releases/tag/v0.5.1

## [0.5.0] - 2026-07-04

### 新增
- **Codex 授权文本 provider**（`codex`）：走本机 `~/.codex/auth.json` OAuth → Codex Responses API 生成文本，用 ChatGPT 订阅、零 API 费。默认 **gpt-5.5 + 思考 high**（文字模型，与图片 `gpt-image-1-mini` 分开）。**支持深度读图**——PDF 作 `input_file`（base64 data URI）随请求发出，gpt-5.5 直接读图片页，实测能准确描述封面色/章节色/书法字体等只有看图才知道的细节。模型配置页文本 Provider 新增 `codex` 选项 + 「已检测到 Codex 授权」徽章；`LLMSetting.codex_model` 可配。

### 修复
- **模型配置页交互**：授权模式（`claude-cli` / `codex`）下正确隐藏 Base URL / Model / API Key（改用内联 `style.display` 切换——修 Tailwind `grid` class 的 `display:grid` 盖掉 HTML `hidden` 属性、导致 API 字段一直显示的旧坑）。
- **保存反馈**：保存配置后跳转带 `?saved=1`，页面弹出「✓ 配置已保存」提示条，2.5s 自动消失。

[0.5.0]: https://github.com/semiok/tnalpha/releases/tag/v0.5.0

## [0.4.0] - 2026-07-04

### 新增
- **①知识库 v2 · 品牌库定义 + 资料文档 AI 解析**（MET-12 续）：正式环境交互重构——默认单品牌「敦煌当代美术馆」，品牌库管理进入 tngen 式**品牌定义页**；主页 campaign 列表可新增、数据池管理入口、campaign 可引用数据池内容。**资料文档 AI 解析照搬 tngen**：单篇文档解读（只读文字）+ **深度读图**（勾选后 claude 读 PDF 图片页出视觉风格）→ 综合成「文档解读综合」+「综合视觉风格」→ 自动填入「主题调性 / 内容要求」，定义者可改可存。默认模型 = Claude。
- **按模块配置模型（预留接口）**：`LLMSetting` 按 `scope` 分行，`scope="default"` 为默认锚点（=知识库），**未配置的模块自动继承默认**；`llm.generate_text/generate_image` 加 `module=` 参数按模块路由，文本/图像各自判断来源。未来模块两步即接入（调用处传 `module=` + 存一行 scope），resolver 不改。无 claude CLI 的贡献者改用 minimax provider 即可（见 `ARCHITECTURE.md §3` / `settings.py`）。
- **六模块导航进开发模式** + ②③④⑤⑥ 占位骨架（`app/modules/{topic,writing,schedule,feedback,permissions}/`）：菜单已连通，各模块开发者往对应目录填功能。

### 修复
- **claude-cli provider 真解析可用**：识别 claude 把认证错误打到 stdout（rc=0）的情况并回退 stub、不把 `Failed to authenticate` 当解读存库；`stdin=DEVNULL` 避免 headless 进交互；深度读图走 `-p ... --allowedTools Read` 读 PDF 图片页。**launchd 服务用独立 `CLAUDE_CONFIG_DIR` 登录**解决与其他 claude 会话争抢 OAuth token 导致的 401（订阅支持多会话）。

[0.4.0]: https://github.com/semiok/tnalpha/releases/tag/v0.4.0

## [0.3.0] - 2026-07-03

### 新增
- **文本/图像模型 API 独立配置**（MET-16，@lindong）：模型配置页把文本与图像模型的 Base URL / Model / API Key 分开保存、分别打码、留空不覆盖、互不影响。新增 **MiniMax 图像 provider**（`image-01`，走 `core/llm` 抽象）+ 文本 `minimax-m3`（OpenAI 兼容）；选 MiniMax 自动填预设。

### 修复
- **补齐 `llmsetting.image_*` 三列的 Alembic 迁移**（带 `server_default`，兼容已有数据的库），移除启动时 `ALTER TABLE` 补列的 hack——schema 回归 Alembic 版本管理，消除迁移历史与模型的漂移。

[0.3.0]: https://github.com/semiok/tnalpha/releases/tag/v0.3.0

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
