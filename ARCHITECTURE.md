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
- **llm**：`core/llm` 暴露统一函数 `generate_text(prompt, task, pdf_path=None, module="default") / generate_image(prompt, module="default")`。**含 stub provider**，没接真实模型时返回假数据，保证端到端能跑。模块不直接调 API。
  - **按模块配置模型（契约）**：`module=` 决定用哪套模型；配置存 `LLMSetting`（按 `scope` 分行）。`scope="default"` 是默认锚点（当前=知识库）；**未配置的模块自动继承 default**。你的模块要用单独的模型，只需：① 调用处传 `module="<你的模块目录名>"`；② 存一行 `LLMSetting(scope="<模块名>", ...)`（`text_provider`/`image_provider` 填 `"inherit"`/留空=继承 default）。resolver（`resolve_llm_settings`）不用改。文本与图像各自判断来源——写作引擎可"文本继承知识库、图像用自己那套"。
  - **无 claude CLI 的贡献者**：本机没 claude 命令时，别用 `claude-cli` provider（会回退 stub）。到「模型配置」页把 Provider 换成 `minimax-m3` 或 `openai`（填自己的 Base URL/Model/API Key）；本地 DB 与维护者隔离，互不影响。
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

### 5.1 ②选题库怎么用①知识库（选题输入的分层消费·必读）

知识库已把原始资料嚼碎成**结构化 digest**——②**直接消费 digest，不要再去读原始 PDF/文档重新理解**（那是①的活，重复浪费）。①负责"理解资料"，②负责"从理解里生成选题"。

**三层输入，各司其职**：

| 层 | ①提供的字段 | ②怎么用 |
|---|---|---|
| **品牌定义**（低时效底座·约束层） | `Brand.brand_prompt`(主题调性)、`content_notes`(内容要求)、`doc_digest`(文档综合)、`style_digest`(视觉风格) | 所有选题的**调性/文风/受众/配图基线**——管"**怎么写**"。换活动也不变。 |
| **活动选题简报**（高时效·内容层） | `Campaign.campaign_digest`（6 块：①活动速览 ②时效节点 ③选题方向 ④关键素材 ⑤配图 ⑥参考与经验） | 这次的**主题/素材/时机**——管"**写什么·什么时候·用什么料**"。 |
| **数据池**（调优层） | `PoolTopic`：`kind='资料包'`(素材佐证) / `kind='经验包'`(⑤复盘的打法) | 素材补充 + **经验包调选题优先级**（优先做/规避），⑤→②闭环。 |

**建议的选题数据流**：
1. 定模式：活动选题（选了 campaign）/ 品牌常青选题（只读品牌层，`campaign_id` 可空）
2. 拿种子：活动模式→**直接采纳 `campaign_digest` 的③选题方向**（已是半成品，标了受众·时效）；品牌模式→从 `doc_digest`+经验包生成
3. 配料：用 `campaign_digest` ④关键素材，给每条选题挂具体可引用的料（尺寸/参数/来源）
4. 定档：用 ②时效节点，给每条标"发布时机+时效优先级"（喂④排期版）
5. 校验：用 `content_notes`（字数/平台/受众/史料/尾注）过一遍
6. 调优：用经验包（优先做/规避）调权重
7. 配图：用 `style_digest`+⑤配图素材，给每条标配图方向

**别踩的坑**：❌ 再读原始文档重新解析（①嚼碎过了）；❌ 把品牌母题/理念当选题内容重复（品牌是约束不是内容）；❌ 忽略②时效节点（丢了高时效价值）；❌ 不读经验包（丢了闭环）。

> 一句话：**品牌层是约束（怎么写），活动层是内容（写什么），数据池经验包是调优（优先级）。② 把三层叠加，`campaign_digest` 的③选题方向就是你的起点。**

### 5.2 ②选题库 ↔ ③写作引擎 的交接（状态归属·后续做，现在留口）

**②只驱动前两态；下游态归③的写作库。** 别把「已创作/已发布」写进 `Topic.status`，那是③（lindong）的库持有的，②读回展示。

| 谁 | 拥有/写 | 读 |
|---|---|---|
| **②选题库** | `Topic.status` ∈ {候选, 采纳}（生成/采纳/取消采纳） | ③回写的下游状态（展示「已创作/已发布」分类） |
| **③写作引擎**（lindong） | 自己的写作库（按 `topic_id` 存写作/图文/发布生命周期：写作中→图文完成→已排期→已发布） | ②的 `status=='采纳'` 选题（接手写作的入口） |

**交接协议（③落地时按此实现，现在不建）**：
1. **③读②**：查 `Topic where status=='采纳'` 作为待写作队列（`topic_id` 是主键，③库外键指它）。
2. **③写自己的库**：写作/发布状态存③库，`topic_id` 关联，**不改 `Topic.status`**（避免两库双写打架）。
3. **②读③**：②的「已创作/已发布」分类 tab 需一个契约函数（如 `writing_status_map(topic_ids)->{id: 状态}`，③提供），②按它归类。**③未接入前该函数视为空 → 这两 tab 为空**（当前即此状态，非 bug）。
4. **守卫**：③已接手（写作库有记录）的选题，②的「取消采纳/删除」应被拒——待③接入后在 `unadopt/delete` 加检查（现无③，暂不加）。

> 一句话：**采纳线（含）以左是②的，以右是③的写作库；② 通过 topic_id + 一个只读契约函数看到右边，绝不双写。**

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
