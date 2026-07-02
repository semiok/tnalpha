# ARCHITECTURE — tnalpha 技术框架

> CTO 技术规范。定义技术栈、目录结构、`core/` 共享底座、**六模块间的接口契约**。所有模块必须遵守本文的契约，否则合不拢。配合 `CLAUDE.md`（规则）+ `DESIGN.md`（UI）。

## 1. 技术栈

| 层 | 选型 | 说明 |
|----|------|------|
| 语言 | Python 3.12 | venv + requirements.txt |
| Web | FastAPI | 路由/依赖注入 |
| ORM | SQLModel | 表即模型 |
| DB | **SQLite（开发）/ Postgres（生产）** | 靠 `TNALPHA_DATABASE_URL` 切；同一套代码 |
| 迁移 | Alembic | schema 改动必须建迁移 |
| 前端 | Jinja + HTMX + Alpine.js | 服务端渲染；富交互用 Alpine |
| 富组件 | SortableJS（拖拽）/ ECharts（图表） | 按需 CDN 引入 |
| 鉴权 | 三角色 RBAC（`core/auth`）| hmac cookie + require_level |
| LLM | `core/llm`（claude 文 + Codex 图）| 统一入口，配置集中 |
| 搜索源 | `core/sources`（适配层）| stub 先行，真实源后接 |

## 2. 目录结构

```
tnalpha/
├─ app/
│  ├─ main.py            应用入口：中间件 + 注册各模块 router
│  ├─ core/              共享底座（维护者管，模块勿改）
│  │  ├─ config.py       DATABASE_URL / SECRET_KEY / USERS
│  │  ├─ db.py           engine / get_session / init_db
│  │  ├─ auth.py         三角色 RBAC（token/current_role/require_level）
│  │  ├─ auth_routes.py  登录/登出
│  │  ├─ llm/            LLM 调用抽象（provider 接口 + stub + claude/codex）
│  │  ├─ sources/        搜索源适配层（SourceAdapter 接口 + stub + 各源）
│  │  ├─ storage.py      文件存储抽象（本地磁盘起步）
│  │  └─ tasks.py        后台任务抽象（线程起步）
│  ├─ modules/           各模块（每人一个子目录）
│  │  ├─ knowledge/  ①   topic/  ②   writing/  ③
│  │  ├─ schedule/   ④   feedback/  ⑤   （权限在 core/auth）
│  └─ templates/
│     ├─ base.html       固定设计 token
│     ├─ components/ui.html  组件宏库（所有模块复用）
│     └─ <模块>/         各模块模板
├─ migrations/           Alembic
├─ tests/                测试
├─ CLAUDE.md DESIGN.md CONTRIBUTING.md 需求文档.md
```

## 3. core/ 共享底座（模块依赖，勿改）

- **db**：`get_session`（FastAPI 依赖，每请求一个 Session）；`init_db` 建表（导入所有模块 models）。
- **auth**：`require_level(request, n)` 服务端守卫（level 不足抛 403）；中间件已把 `request.state.role/level` 塞好，模板直接用。
- **llm**：`core/llm` 暴露统一函数（如 `generate_text(prompt, task) / generate_image(...)`）。**含 stub provider**，没接真实模型时返回假数据，保证端到端能跑。模块不直接调 API。
- **sources**：`core/sources` 暴露 `SourceAdapter` 接口 + 注册表 + `search(source, query)`。**含 stub adapter** 返回假热点/笔记。真实源（热点/小红书/公众号/网络）按接口后接。
- **storage / tasks**：文件存储、慢任务的抽象接口（本地磁盘 / 线程起步）。

## 4. 数据模型全景（跨模块实体关系）

**核心主线：品牌 → campaign → 选题 → 图文 → 排期 → 反馈**

```
Brand (①)
 └─ Campaign (①)  [is_default=品牌日常]
      ├─ CampaignDoc (①)
      ├─ Topic (②)            campaign_id FK → Campaign
      │    ├─ Article (③)     topic_id FK → Topic
      │    │    └─ ArticleImage (③)
      │    ├─ ScheduleSlot (④) topic_id FK；publish_date
      │    └─ MediaStats (⑤)  topic_id FK
      └─ Style (③)            campaign_id FK；is_default

DataPool (①) — 独立/全局
 └─ PoolTopic (①)  kind=资料包|经验包；web_access(触网/不触网)；source
      ↑ 经验包由 ⑤ 写入（Insight 沉淀）；被 ② 读取
```

## 5. ★ 模块间接口契约（最重要，必须对齐）

> 谁定义表、谁只读、外键指向谁——定死，避免各建各的。

| 契约 | 拥有者（建表/写） | 消费者（读/引用） |
|------|------------------|------------------|
| **Brand / Campaign** | ①知识库 | ②③④⑤ 都按 `campaign_id` 引用 |
| **Topic**（选题） | ②选题库（含 `campaign_id`, `status`, `type`）| ③按 topic_id 生成文章；④排期；⑤回填数据 |
| **Article / ArticleImage** | ③写作引擎（`topic_id`）| ④⑤ 展示"看文章" |
| **Style**（写作风格） | ③写作引擎（`campaign_id`, `is_default`）| — |
| **ScheduleSlot**（排期） | ④排期版（`topic_id`, `publish_date`, 发布信息）| ⑤读发布信息 |
| **MediaStats**（回填数据）| ⑤数据反馈（`topic_id`）| — |
| **DataPool / PoolTopic**（数据池·经验包）| ①建表；**⑤写入经验包**（`kind='经验包'`, `source='feedback'`）| **②读取**（选题推荐参考）|

**关键约定**：
- **选题状态机**（②拥有，③④⑤读）：`候选 → 采纳(待写作) → 写作中 → 图文完成 → 已排期 → 已发布`。字段名统一 `status`。
- **数据池是同一张表**：①建 `PoolTopic`，⑤往里写 `kind='经验包'` 的行，②查询它。三方对齐字段（见下 §6 表定义），不许各建各的。
- **触网/不触网**：`web_access` 字段在 `PoolTopic`；本期生成统一走云端（记录但不强制隔离）。
- **跨模块只读引用用 FK + 查询，不复制数据**。

## 6. 关键共享表字段（① 定义，②⑤ 遵守）

```python
# core 或 knowledge 模块定义，②⑤ import 使用，不要重建
class PoolTopic(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    title: str
    kind: str           # '资料包' | '经验包'
    web_access: bool = True          # 触网/不触网
    source: str = 'upload'           # 'upload' | 'feedback'(来自⑤) | 'shared'
    brand_tag: str | None = None     # 来源品牌 tag（可空=通用）
    content: str = ''                # 解析结果/经验摘要
    created_at: datetime
```
⑤ 沉淀经验包 = 插入一行 `PoolTopic(kind='经验包', source='feedback', brand_tag=<品牌>, content=<复盘摘要>)`。
② 选题推荐 = 查询 `PoolTopic.where(kind=='经验包')` 相关行喂进 prompt。

## 7. 权限矩阵（⑥ 汇总，各模块落地）

三角色严格包含：owner(2) ⊃ editor(1) ⊃ publisher(0)。

| 动作 | owner | editor | publisher |
|------|:--:|:--:|:--:|
| 知识库 增删改/上传/数据池治理 | ✓ | ✗ | ✗ |
| 浏览/下载 | ✓ | ✓ | ✓ |
| 选题 推荐/采纳/删 · 写作 · 排期 | ✓ | ✓ | ✗ |
| 发布 + 回填数据 | ✓ | ✓ | ✓ |
| 看板/复盘/看文章 | ✓ | ✓ | ✓ |

服务端 `require_level(request, n)` + 模板 `request.state.level >= n` 双层。

## 8. 闭环数据流

```
①知识库(素材) → ②选题(多源推荐,读数据池经验包) → ③写作(注入风格)
→ ④排期(AI推荐+发布回填) → ⑤数据反馈(回填+复盘)
→ ⑤沉淀经验包写入 DataPool → 回到 ② 下轮推荐更准
```

## 9. 演进方向（留了口子，暂不做）

- 搜索源：stub → 真实 adapter（找到可用 API 再接，接口不变）
- 存储：本地磁盘 → 对象存储（storage 接口已抽象）
- 后台任务：线程 → 任务队列（tasks 接口已抽象）
- LLM：全云端 → 不触网料走本地模型（llm 层加路由）
