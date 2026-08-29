# 实体 JSON Schema 契约

本目录是**系统数据的正式契约**（"先契约、后实现"，docs/07 的约定）。bible/outline/memory 是持久事实源文件（docs/06），这些 schema 是它们写入前校验、一致性规则引擎引用依据、以及 Pydantic 建模输入（docs/06 §5）。

## 目录

- `bible/` — 设定圣经（权威·应然）：`worldview` / `characters` / `locations` / `timeline` / `plot_threads` / `style`
- `outline/` — 大纲：`volume`（卷级）、`chapter_gist`（章节细纲 front-matter）
- `memory/` — 记忆层（事实·实然）：`character_history` / `plot_event` / `relationship` / `fragment_index`
- `config.schema.json` — 全局配置（TOML → Pydantic，docs/08 配置层）
- `policy.schema.json` — 权限门禁策略（docs/07 §3.4）
- `project.schema.json` — `project.json` 项目元数据 + 流水线状态 + 批基线
- `events.schema.json` — 事件总线事件（docs/07 §4.1）
- `llm.schema.json` — LLM 适配器请求/响应契约（docs/07 §2）

## 使用约定

- schema 用对应实体的 `$id` 定位；实现用 Python `jsonschema` 或 Pydantic v2 校验。
- 一旦 schema 定型，改动须连同 docs/06 §5.2 的"规则引擎白名单"一起评审（避免引擎不认识新字段）。
- `src/novelist/storage/schemas/` 指向本目录（或复制），编码阶段以本目录为源。
