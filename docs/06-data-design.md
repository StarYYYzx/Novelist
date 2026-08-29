# 06 · 数据与存储设计

> 定义**小说工作区**（Novel Workspace）的目录规约、数据模型与 JSON Schema。工作区是系统长期状态的"单一事实源"（ADR-004），文件即可 Git 版本化、可人工编辑。

## 1. 设计目标
- 文件层级稳定、可人工阅读、可 diff（Markdown / JSON）。
- 实体（人物、地点、伏笔……）集中且唯一，避免多头。
- 正文与编辑产物分离（草稿 vs 正式）。
- 支持检查点快照与 Git 版本管理双轨（ADR-010）。

## 2. 工作区目录规约

```
novel_workspace/
└── <project>/                        # 一个项目一本书
    ├── project.json                  # 元数据：书名、作者、目标、工期、偏好、预算
    ├── bible/                        # 设定圣经（单一事实源）
    │   ├── worldview.json            # 世界观：体系、文明、设定规则
    │   ├── characters.json           # 人物卡（数组）
    │   ├── locations.json            # 地点/地图
    │   ├── timeline.json             # 时间线事件
    │   ├── plot_threads.json         # 伏笔登记与回收
    │   └── style.json                # 文风约束、禁用词、术语表、叙述偏好
    ├── outline/                      # 大纲（结构化）
    │   ├── volumes.json              # 卷级大纲
    │   └── chapters/<vol>-<ch>.md    # 章节细纲（每章一文件）
    ├── drafts/                       # 正文草稿（未批准）
    │   └── chapters/<vol>-<ch>.md
    ├── chapters/                     # 正文（已批准/正式）
    │   └── <vol>-<ch>.md
    ├── workspace/                    # 编辑中间态（主编剧 scratchpad、子代理临时）
    ├── reports/                      # 审查报告、一致性告警、统计
    │   ├── alerts/
    │   └── stats/
    ├── logs/                         # 审计与事件日志
    └── .checksum.json                # 关键文件 hash 索引（用于一致性定位）
```

## 3. 核心实体数据模型

> 全部采用 JSON（除章节正文为 Markdown）。以下为字段示意，正式 Schema 见 §5。

### 3.1 人物卡 `characters.json`
```json
{
  "id": "char:cz7",                  // 稳定ID，全书记引用
  "name": "苏晚",
  "aliases": ["阿晚"],
  "species": "human",
  "core_traits": ["谨慎", "外冷内热"],
  "power": {"level": "九阶", "faction": "青云宗"},
  "arc": "从弃徒到掌门",
  "first_appear": {"vol": 1, "ch": 3},
  "status": "active",
  "relationships": [{"target": "char:wm2", "type": "mentor"}]
}
```

### 3.2 伏笔 `plot_threads.json`
```json
{
  "id": "pt:V017",
  "desc": "苏晚袖中的断玉佩",
  "planted": {"vol": 2, "ch": 5},
  "status": "planted",               // unplanned|planted|pending_return|returned
  "report_deadline": {"vol": 4, "ch": 0},   // 逾期提醒（F4.3）
  "returned": null
}
```

### 3.3 时间线 `timeline.json`
```json
{
  "id": "tl:13",
  "at": {"era": "青云纪", "year": 3, "season": "秋"},
  "event": "苏晚继任掌门",
  "in_chapters": [{"vol": 1, "ch": 3}]
}
```
> 一致性规则要求：正文中出现的时序事件均映射到 `timeline`，保证单调不矛盾（F4.1）。

### 3.4 章节细纲 `outline/chapters/<vol>-<ch>.md`
Markdown 头信息 + 正文要点：
```markdown
---
id: ch:1:3
vol: 1
ch: 3
title: 断玉
pov: demo
key_events: [接受试炼, 发现断玉佩, 卷入宗门内斗]
turns: [opening-hook, setup, conflict, cliffhanger]
threads_involved: ["pt:V017"]
---

## 细纲要点
- 开场钩子：……
- 冲突主线段落：……
- 结尾悬念/钩子：……
```

## 4. 状态机（流水线 / 正文 / 伏笔）

### 4.1 工序状态机（F1.1）
```
立项 → 世界观 → 大纲 → 细纲 → 正文 → 审查 → [待发布]
                    ↑________ 修订 ______↓ (任何一层的修订回流)
```
- 每节点可被人工"暂停/编辑/续跑"，状态持久在 `project.json.pipeline_state`。
- 伏笔逾期、人工跳步等事件会自动把部分工序拉回 `修订`。

### 4.2 章节正文状态
```
planned(细纲完成) → drafting → draft_ready → reviewing → reviewed_ok → published
                                  └─reviewed_fail──→ revising ─→ reviewing
```
- `draft_ready → published` 间的 `promote` 为 sensitive 操作（可走门禁）。

### 4.3 伏笔状态
`unplanned → planted → pending_return → returned`（F4.3）。

## 5. JSON Schema 约定（正式契约）

每个 Bible 文件有独立 JSON Schema，用途：
1. 工具写入前的校验；
2. 一致性**规则检引擎**的字段引用依据；
3. 生成实现时的 dataclass/Pydantic 建模输入。

> Schema 的完整定义将在实现阶段以 `schemas/*.schema.json` 提供（见 08 规划）。本文档定结构，不铺开全部字段。

### 5.1 通用 schema 骨架
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "character",
  "type": "object",
  "required": ["id", "name", "status"],
  "properties": {
    "id": {"type": "string", "pattern": "^char:[A-Za-z0-9_-]+$"},
    "name": {"type": "string", "minLength": 1},
    "core_traits": {"type": "array", "items": {"type": "string"}},
    "status": {"enum": ["active", "dead", "away", "unknown"]}
  }
}
```

### 5.2 引用完整性（一致性规则引擎输入）
- `characters.*.id`、`locations.*.id`、`plot_threads.*.id` 为全局唯一索引。
- 正文章节头中的 `threads_involved`、人物 mention 等，均需命中对应实体 id（缺失即告警）。
- schema 变化需与规则引擎白名单同步，避免"引擎不认识新字段"。

## 6. 检查点（ADR-010 双轨）
- **版本轨（Git 友好）**：`bible/`、`outline/`、`chapters/` 为可 diff 文本文件，改动即有版本历史。
- **快照轨（程序恢复）**：`project.json` 含 `pipeline_state` 指针 + 各实体文件 hash；编排器在工序边界写入 `.checksum.json`，作为可恢复快照。
- 恢复逻辑：`project.json` + `.checksum.json` 校验一致 → 重建状态；不一致则以 Git/日志回溯。

## 7. 容量与并发约束（设计假设）
- 单文件（人物卡/地点/伏笔）规模适中，全量读入上下文可接受；超限时用 04§5.3 压缩。
- 批量并行写草稿互不冲突（每章独立文件），同时写 `project.json` 的原子更新用"临时文件 + rename + 锁"。
- 目录路径全部相对沙箱根，禁止 `..`（安全约束）。

## 8. 版本策略
- 每个 `bible/` 实体更新自增版本号（`revision` 字段），与 `.checksum.json` 联动，保证一致性引擎能定位"引入了哪次改动的章节"。
