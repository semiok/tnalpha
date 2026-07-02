# tnalpha UI / 交互规范

> **所有模块的界面必须遵守本规范 + 复用 `app/templates/components/ui.html` 里的组件宏。**
> 不许自创按钮/卡片/配色。AI 开发模块时，先读这份，再照 `app/modules/knowledge/`（样板）抄。

---

## 1. 设计 Token（颜色 / 字体 / 间距）

Token 固定在 `base.html` 的 Tailwind 配置里，全站用 class 引用，**改一处全局生效**。

| 用途 | Token / class | 值 |
|------|--------------|-----|
| 主色（操作/强调） | `brand` → `bg-brand-600` `text-brand-600` | indigo 600 `#4f46e5` |
| 成功/已完成 | `emerald-600` | 绿 |
| 警示/时效 | `amber-500` | 琥珀 |
| 危险/删除 | `rose-600` | 红 |
| 文字主色 | `text-slate-800` | — |
| 次要文字 | `text-slate-500` | — |
| 页面背景 | `bg-slate-100` | — |
| 卡片背景/描边 | `bg-white` `border-slate-200` | — |
| 圆角 | 卡片 `rounded-xl`、按钮 `rounded-lg`、标签 `rounded-full` | — |
| 字体 | 系统字体（已在 base 设好，含 PingFang SC） | — |

**模块色**（每个模块在自己页面的强调色，仅用于图标/标签点缀，不改主操作色）：
①知识库 sky · ②选题库 violet · ③写作 fuchsia · ④排期 amber · ⑤数据 emerald · ⑥权限 indigo。

---

## 2. 组件库（必须复用）

全部在 `app/templates/components/ui.html`，用 Jinja 宏。用法：
```jinja
{% from "components/ui.html" import button, card_open, card_close, chip, page_header, empty_state, modal_open, modal_close %}
{{ page_header("知识库", "敦煌IP", crumbs=[("首页","/"), ("敦煌IP","/brands/1")]) }}
{{ button("保存", variant="primary", type="submit") }}
{{ button("删除", variant="danger", hx_post="/x/delete", hx_confirm="确定删除？") }}
{{ chip("已发布", color="emerald") }}
```

| 组件宏 | 用途 | 关键参数 |
|--------|------|---------|
| `page_header(module, title, crumbs)` | 每个页面顶部：模块名 + 标题 + 面包屑 | crumbs=[(文字,链接)…] |
| `button(label, variant, ...)` | 按钮 | variant: primary/secondary/danger/ghost；hx_* 透传 |
| `card_open(title, actions)` / `card_close()` | 白卡片容器 | — |
| `chip(label, color)` | 小标签 | color: slate/emerald/amber/rose/brand |
| `field(label, name, type, value)` | 表单项（label+input，竖排） | — |
| `text_input(name, placeholder, type, required, extra)` | 独立文本/日期输入（内联表单用）| type: text/date…；extra: 附加 class 如 w-full |
| `textarea(name, placeholder, rows)` / `select_input(name, options)` / `file_input(name, accept, required)` | 独立 textarea / 下拉 / 文件 | select 的 options=列表 |
| `table_open(headers)` / `table_close()` | 表格 | headers=[…] |
| `modal_open(title)` / `modal_close(footer)` | 弹框 | — |
| `empty_state(text)` | 空列表占位 | — |

新组件需求 → 加进 `ui.html` 并更新本表，**不要在模块里写一次性样式**。

---

## 3. 交互约定（HTMX + Alpine）

统一用 **HTMX**（服务端渲染、局部刷新）+ **Alpine.js**（轻状态，如弹框开关）。

| 场景 | 标准做法 |
|------|---------|
| 列表增删改后刷新 | HTMX `hx-post/delete` + `hx-target` 指向列表容器 + `hx-swap="outerHTML"`，后端返回该列表 partial |
| 危险操作（删除/发布） | 必须 `hx-confirm="…后果说明…不可恢复"` 弹框确认 |
| 弹框 | 放进全局 `#modal-slot`，`hx-target="#modal-slot" hx-swap="innerHTML"`；关闭清空 |
| 保存成功提示 | 后端响应头 `HX-Trigger: saved` → base.html 统一弹 toast |
| 慢任务（AI 生成/抓取） | 立即返回"生成中"占位 + `hx-trigger="load delay:3s"` 轮询状态片段（见知识库/写作样板）|
| 按钮 type | 非提交按钮一律 `type="button"`（避免误提交）|
| 表单收集多选 | 用 `hx-include="[name='xxx']"`，不强制 `<form>` 包裹 |
| 权限显隐 | `{% if request.state.level >= N %}…{% endif %}`（见第 5 节）|

**加载态**：慢操作要给反馈（spinner 或"生成中…"），不能点了没反应。

---

## 4. 页面骨架（每个模块页都长这样）

```
{% extends "base.html" %}
{% from "components/ui.html" import page_header, button, card_open, card_close %}
{% block content %}
  {{ page_header("②选题库", "敦煌当代美术展", crumbs=[("首页","/"),("敦煌IP","/brands/1"),("美术展","/campaigns/9")]) }}
  {{ card_open("推荐控制") }}
     …
  {{ card_close() }}
{% endblock %}
```
- 内容最大宽度、间距由 base 统一控制，模块内不要自定义页面级布局。
- 一级页面 + 二级详情页都走同一套 header + card 结构。

---

## 5. 角色与显隐（接 ⑥权限）

三角色 level：定义者 owner=2 / 选题者 editor=1 / 发布者 publisher=0。
- 模板里按 `request.state.level >= N` 决定按钮显隐（N 见各模块 spec）。
- **UI 隐藏 + 服务端 `require_level` 双层**，UI 只是体验层，安全靠服务端。

---

## 6. 文案风格

- 中文为主，技术名词保留英文（campaign / KOL）。
- 按钮用动词（"保存""推荐选题""生成文章"）。
- 危险操作确认文案要说清后果（"其图文/数据将一并删除，不可恢复"）。
- 空状态给引导（"还没有选题，点『推荐选题』开始"）。

---

*本规范随组件库演进。改 token / 加组件 → 同步更新本文件 + `ui.html`。*
