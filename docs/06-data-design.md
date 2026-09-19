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
    │   ├── style.json                # 文风约束、禁用词、术语表、叙述偏好
    │   ├── worldstate.json           # 世界状态（硬状态层）：人物当前修为/位置/持有物/伤势
    │   ├── items.json                # 物品注册表（可持有实体，游戏式资产登记）
    │   ├── skills.json               # 功法/技能注册表（可学习实体，与物品分离）
    │   ├── settings.json             # 设定条目库（按知识单元切块 + 首次交代状态）
    │   └── review_lessons.json       # 审校历史教训（block 沉淀，生成时注入防重犯）
    ├── outline/                      # 大纲（结构化）
    │   ├── volumes.json              # 卷级大纲
    │   └── chapters/<vol>-<ch>.md    # 章节细纲（每章一文件）
    ├── drafts/                       # 正文草稿（未批准）
    │   └── chapters/<vol>-<ch>.md
    ├── chapters/                     # 正文（已批准/正式）
    │   └── <vol>-<ch>.md
    ├── workspace/                    # 编辑中间态（主编剧 scratchpad、子代理临时）
    ├── memory/                       # 记忆子系统（ADR-011，事实演进·实然）
    │   ├── character_histories/<char>.json   # 人物经历史（按角色）
    │   ├── plot_events.json          # 剧情事件流（全书事件按序）
    │   ├── relationships.json        # 角色关系变化记录
    │   ├── fragment_index.json       # 记忆碎片元数据（来源定位/引用 bible id/hash）
    │   └── rag/                      # 检索索引落盘（embedding 向量/倒排，可再生）
    ├── takes/                        # 角色演员试演片段（临时素材，随章归档）
    │   └── <vol>-<ch>/<char_id>_n.md
    ├── .index.db                     # SQLite 辅助索引/查询通道（ADR-016，可由文件重建）
    ├── reports/                      # 审查报告、一致性告警、统计
    │   ├── alerts/
    │   └── stats/
    ├── logs/                         # 审计与事件日志
    ├── tasks/                        # 任务板（ADR-028，细粒度任务持久化/崩溃单任务恢复）
    │   └── ch_1-2.json …             # 每卷/章一任务 JSON（id/kind/status/owner/dependencies）
    └── .checksum.json                # 关键文件 hash 索引（用于一致性定位）
```

> **2026-09-19 增补**（综合审计对齐）：上树之外的在盘文件族——
> `bible/lines.json`（线索卡）、`bible/entity_progress.json`（实体进度实然缓存，无 schema）、
> `bible/review_lessons.json`（审校教训）、`bible/settings_pending.json` /
> `character_needs_pending.json`（JIT 待审）、`outline/arcs.json`（arc 层，无 schema）、
> `workspace/forge/`（blueprint.json / transcript.jsonl / nodes/ / snapshots/ / review.json /
> conflicts.json / doctor.md / report.md）、`workspace/agent/session.jsonl`（对话持久化，ADR-036）、
> `workspace/feedback/ops.json`、`memory/directions/`、`memory/castings/`、
> `drafts/*.src.json`（溯源双轨）、`reports/reviews/`（细纲连读审查）。
> 其中无 schema 的六个文件见《综合工程审计与决策清单-2026-09-19》D-3（待拍板是否纳入契约）。

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
  "intent": "洗清冤屈，夺回苏家渊佩",   // dp-intent：此刻最执着的欲望（动机着色的权威源）
  "plan": "先回青源坊查证当年卷宗再赴藏剑阁",  // dp-intent：近段计划
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

### 3.3 时间线与定时事件（ADR-019，2026-09-01 拍板）

> 原设计的历法式 `at: {era, year, season}` 已废弃——LLM 维护历法必然漂移，改为**相对天数轴**。

#### 3.3.1 `bible/timeline.json` — 历史时点登记簿（事实源，ADR-016）

```json
[
  {"id": "tl:13", "event": "叶蓝服丹闭关", "at": {"t": 1143, "vol": 1, "ch": 12},
   "in_chapters": [{"vol": 1, "ch": 12}]},
  {"id": "tl:14", "event": "叶蓝出关，突破筑基", "at": {"t": 1233, "vol": 1, "ch": 15},
   "in_chapters": [{"vol": 1, "ch": 15}]}
]
```

- `t` 为**相对天数**（开书之日 = 0），章序 `(vol, ch)` 只是 t 的粗粒度投影。
- 写入方：编纂员事件回写链路（"时间："行 → `LandedEvent.timeline_delta` → 落盘；
  该接口 `writeback.py:41` 此前定义了但零调用方，本机制激活它）。
- R-TL 单调性校验**按 t**（原按章序首现位置）。

#### 3.3.2 `bible/worldstate.json` 扩展 — 当前时刻与未来日程

```jsonc
{
  "time": {"now": 1143, "origin_text": "叶蓝穿越之日"},
  "pending": [
    {"id": "pd:1", "who": "char:yelan",
     "what": "叶蓝出关（服丹闭关三月，可能突破筑基）",
     "due": 1233,                       // 天数轴上的到期时刻
     "span": 90,                        // 跨度（due − 登记时 now），分档比例分母
     "created_t": 1143,                 // 登记时刻（天）
     "status": "scheduled",             // scheduled|fired|cancelled|expired
     "created_at": {"vol": 1, "ch": 12},
     "overdue": 0,                      // 到期后经过的章数（tick 记账）
     "block_count": 0,                  // 软 block 次数（≥3 自动 expired）
     "thread": "pt:V017"}               // 可选：与伏笔 id 互引（不合并，见下）
  ],
  "characters": { /* 既有结构；约定文本命中 `unavailable_states` 词表
                     （闭关/失踪/昏迷/被囚/渡劫，worldview.unavailable_states 可覆盖）
                     → 该人物 `unavailable_until = due` + `unavailable_since = {vol,ch}` */ }
}
```

**pending 状态生命周期**（2026-09-01 拍板）：`scheduled → fired`（正文引出）；
`scheduled → expired`（R-TIME 连续 3 次 block 后**自动**置位放行，report 与告警留痕
"该事件已逾期作废，建议人工处理"——挂机批跑不会永久卡死，事件也不会静默消失）；
`cancelled` 仅由**用户手改** worldstate 置位（系统不代取消）。

**编纂员抽取行扩展**（搭既有事件抽取调用，零新增 LLM 调用；时间行**每段必答 1 条**——C1/D
2026-09-03：正文无跨度即答「时间：同日」，杜绝"模型不答 → now 停滞 → 时间承诺被遗忘"；
约定行**最多 1 条/章**，没有就不写）：

| 输出行 | 语义 | 效果 |
| --- | --- | --- |
| `时间：+90日` | 事件推进故事时间 | `time.now += 90`；登记 timeline 条目（可带备注 `| 事件`） |
| `时间：闪回` | 回忆/插叙 | 不推进 `now` |
| `时间：同日` | 多线"与此同时" | `dt = 0`（只登记时点，不推进） |
| `约定：叶蓝出关｜+90日` | 未来事实登记 | 追加 `pending[]`（id 自动分配，**登记先于推进**：due 按章首 now 算） |

模糊量词归一兜底：数日后→2、半月→15、三月→90、三年→1095（prompt 要求 LLM 给具体数，
归一表兜底抽取噪声）；单事件 dt > 3650 → warn（疑似抽取错误）。

**兑现判定**（`timeline.tick`，章末确定性判定，零 LLM）：token 重合度启发式优先
（`what` 二元组与正文重合率 ≥30%，长文本须 ≥2 个）；**词面不重合兜底**——「闭关三月」与
「出关」没有共同二元组，若 pending 关联人物姓名在正文出场**且已到期**（due ≤ now，此前
强提示已注入 key_events 候选），视为兑现。轻微误判（人物出场但未处理节点）只停止提醒，
损失可接受，符合"到期只升提示强度、不硬插剧情"。

#### 3.3.3 渐进提醒分档（produce_chapter 注入，确定性规则，仿 PhasePolicy）

| 剩余时间（due − now） | 行为 |
| --- | --- |
| > 30% | 静默（仅 worldstate 可查） |
| ≤ 30% | 「临近事项」轻提示（"叶蓝闭关将满"） |
| ≤ 10% 或已到期 | 强提示 + key_events 候选（"本章宜安排：叶蓝出关"） |
| 到期 3 章未处理 | R-TIME warn；再 2 章未处理 → **软 block**（本章 key_events 须引用该 pending 才放行）；连续 3 次 block → 自动转 `expired` 放行 + report 留痕（防死锁） |

**与 §3.2 plot_threads 的边界**：threads 的 `report_deadline` 是**章节位置**驱动（第几卷第几章前回收），
pending 的 `due` 是**天数**驱动（世界日程到点没有）；同一事实（出关既是伏笔回收又是定时事件）
两边登记、经 `pending.thread` 互引，**不合并**。到期触发只升提示强度、不硬插剧情（与 ADR-002
串行逐章、大纲驱动一致）。

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
出场人物: [赵铁山, 鬼面]        # 递归分层 A（JIT 补卡触发器）：生成前扫描缺卡并补全
turns: [opening-hook, setup, conflict, cliffhanger]
threads_involved: ["pt:V017"]
---

## 细纲要点
- 开场钩子：……
- 冲突主线段落：……
- 结尾悬念/钩子：……
```

> 约定（递归分层，M3k）：
> - `key_events` 是**声明式事件清单**，事件文本带 `[expanded]` 后缀 = 重场戏，
>   递归拆 ≤3 拍逐拍生成（拍级带上一拍全文，失败回退事件级）；
> - `出场人物:` 声明本章新出场人物——bible 缺卡时由 `_jit_characters` 在生成前
>   LLM 补全（吸收前文实际发展，滚动设计；出场即建档，防造人红线）；

### 3.5 记忆层数据模型（ADR-011，事实演进·实然）

> bible 定义"人设应当怎样"（应然）；memory 记录"角色经历了什么、事件如何推进"（实然）。每条记忆带**来源定位 + 引用 bible id + 版本**，供一致性定位与 RAG 检索。

#### 人物经历史 `memory/character_histories/<char>.json`
```json
{
  "char_id": "char:cz7",
  "revision": 12,
  "entries": [
    {
      "at": {"vol": 3, "ch": 8},
      "summary": "苏晚在青云试炼中击败大师兄，身受重伤，暗中获得断玉佩认主",
      "state_delta": {"power": "晋升九阶", "status": "有伤在身"},
      "emotion": "由疑惧转向坚定",
      "refs": ["pt:V017", "loc:qingyun"],
      "hash": "a1b2c3"
    }
  ]
}
```
- `entries` 按时间追加；`state_delta` 记录状态增量（人物卡本身在 bible 是基线，经历在此追加）。
- "写作前先忆"即查询此文件 + 检索命中的历史条目。

#### 剧情事件流 `memory/plot_events.json`
```json
[
  {
    "id": "ev:34",
    "at": {"vol": 3, "ch": 8},
    "type": "conflict|discovery|reveal|turning_point|…",
    "summary": "断玉佩在决斗中显形，牵出宗门尘封秘辛",
    "participants": ["char:cz7", "char:bds"],
    "affected_threads": ["pt:V017"],
    "related_events": ["ev:12"],
    "causality_note": "由 5 章埋下的玉佩执念引爆"
  }
]
```
- 作为全书事件链的检索索引，供因果/伏笔回溯。

#### 关系变化 `memory/relationships.json`
```json
{
  "pairs": [
    {
      "a": "char:cz7", "b": "char:bds",
      "entries": [
        {"at": {"vol": 2, "ch": 1}, "from": "敌对", "to": "亦敌亦友"},
        {"at": {"vol": 3, "ch": 8}, "from": "亦敌亦友", "to": "生死与共"}
      ]
    }
  ]
}
```
> 注（2026-09-03）：本文件是"关系变化事件日志"（`MemoryWriter.record_relationship_change`
> 写入，目前无管线调用方）。**实然关系账本**另立 `memory/relationship_ledger.json`
> （ADR-023，core/rel_ledger.py）——从 character_histories 视角 relations delta 确定性聚合的
> **可再生投影**（ADR-016，可删除重建，非事实源），含 pair state/trend/diverged/by/last_event
> 与阈值翻转标记；消费方读账本行时先查它，bible 关系行（应然）仅由 enrich pending 人工
> `--allow` 改写。

#### 记忆碎片索引 `memory/fragment_index.json`
- 每条记忆碎片登记 `id/kind/source(vol,ch)/refs` + 内容 hash，供编纂去重、冲突定位、索引重建增量更新。

## 4. 状态机（流水线 / 正文 / 伏笔 / 记忆）

### 4.1 工序状态机（F1.1）
```
立项 → 世界观 → 大纲 → 细纲 → 正文 → 审查 → 待发布
                    ↑________ 修订 ______↓      (可回流)
```
- 每节点可被人工"暂停/编辑/续跑"，状态持久在 `project.json.pipeline_state`。
- 伏笔逾期、人工跳步等事件会自动把部分工序拉回 `修订`。
- **正文阶段严格串行逐章**（ADR-002 修订），不做批次并行；事件回写在正文编写过程中实时完成（见 04§4.2 / 05§5.4），无须独立的章末"记忆编纂"工序节点。
- **任务明细（细粒度，ADR-028）**：`pipeline_state` 是阶段"总开关"，章下面的"哪一/几章正在写、被谁(owner)写、依赖是否就绪"落在 `tasks/` 任务板里（`TaskStore`），崩溃后可按任务粒度续/重做，而不是整卷回退。

### 4.2 章节正文状态（串行，事件实时回写）
```
planned(细纲完成) → drafting → draft_ready → reviewing → reviewed_ok → published
                                  └─reviewed_fail──→ revising ─→ reviewing
```
- `draft_ready → published` 间的 `promote` 为 sensitive 操作（可走门禁）。
- `reviewed_ok → published`：章节审查通过后转正；事件回写已在写作过程中逐条完成（§4.4），转正即时序收口，无章末补录、无 `pending_release`。
- **无并行批次**：同一时刻至多一章在 `drafting`；串行推进（NFR-1/14）。

### 4.3 伏笔状态
`unplanned → planted → pending_return → returned`（F4.3）。伏笔状态变化在事件回写时实时记入 `plot_events.json` 与 `plot_threads.json` 供检索。

### 4.4 事件回写与索引状态
`event_landed → validated（冲突双检通过）→ indexed（rag 索引增量更新）→ archived`
- 每个事件落定即触发此状态机（ADR-013）：`validated` 失败 → `conflicted` → 人工仲裁 → 通过则 `validated`，否则丢弃（不污染记忆）。
- RAG 索引增量重建可异步，但需在下一事件/下一章 `query_memory` 前完成或标注"待索引"。
- “章末统一编纂”已废除；批/章末补录不存在。

## 5. JSON Schema 约定（正式契约）

每个 Bible 文件有独立 JSON Schema，用途：
1. 工具写入前的校验；
2. 一致性**规则检引擎**的字段引用依据；
3. 生成实现时的 dataclass/Pydantic 建模输入。

> Schema 的完整定义在实现阶段以 `schemas/*.schema.json` 提供（见 08 规划）。本文档定结构，不铺开全部字段。
> **F0'（2026-09-01）契约已与磁盘实然对齐**：数组型文件（characters/locations/items/settings/
> plot_threads/timeline/volumes/plot_events）schema 根节点为 `array`；style 为扁平结构
> （pov/tone[]/target_words_per_chapter/forbidden_words/protagonist，无顶层 style 键）；
> timeline.at 双格式 oneOf（新 `t/vol/ch` | 旧历法 `era/year/season`）；worldview 含
> power_system/phase_policy/unavailable_states/factions；校验入口 `core/bible.py validate_project`
> + CLI `novelist validate`（14 类契约映射，缺失文件不违规）。

### 5.1 通用 schema 骨架
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "character",
  "type": "array",
  "items": {
    "type": "object",
    "required": ["id", "name"],
    "properties": {
      "id": {"type": "string", "pattern": "^char:[A-Za-z0-9_-]+$"},
      "name": {"type": "string", "minLength": 1},
      "core_traits": {"type": "array", "items": {"type": "string"}},
      "background": {"type": "string"},
      "is_protagonist": {"type": "boolean", "default": false},
      "intent": {"type": "string"},   // dp-intent：欲望/动机（动机着色的权威源）
      "plan": {"type": "string"},     // dp-intent：近段计划（随视角快照入记忆）
      "status": {"enum": ["active", "dead", "away", "unknown"]}
    }
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
- 正文**严格串行**推进（同一时刻至多一章在写），草稿按独立文件落地；写 `project.json`、`memory/*.json` 的更新用"临时文件 + rename + 锁"保证原子性（事件回写与正文写可能短时并发提交）。
- 目录路径全部相对沙箱根，禁止 `..`（安全约束）。

## 8. 版本策略
- 每个 `bible/` 实体更新自增版本号（`revision` 字段），与 `.checksum.json` 联动，保证一致性引擎能定位"引入了哪次改动的章节"。

## 9. SQLite 辅助索引（ADR-016）
> 文件（bible/memory/outline/chapters）仍是**持久事实源**；`<project>/.index.db` 仅是加速的**辅助查询通道**，随时可删、可从文件重建。

- 承载表：
  - `fragments`（记忆碎片检索索引：`sig/kind/source/refs/score_fields`——从 `memory/*.json` + 正文重建）；
  - `plot_events`、`relationships`、`character_histories`（从对应 JSON 事实源导入的范围查询视图）；
  - `audit_log`（事件日志的持久化副本，见 F7.1）；
  - `checkpoints`（检查点元信息）。
- **可重建性**：`reindex_memory` / `rebuild_indexdb` 从文件全量重建 `.index.db`；任一条目可用 `.checksum.json` 的 hash 与文件核验一致。
- **一致性约束**：写入路径以"文件为真、SQLite 为冗余镜像"——任何写操作先落文件（原子写）后同步索引；索引缺失/过期时自动触发重建，不影响正确性、只影响查询性能。
- 切换策略：若某部署想回到"纯文件、无 SQLite"，删除 `.index.db` 并把查询退化为文件扫描即可（功能等价，性能或降级）。
