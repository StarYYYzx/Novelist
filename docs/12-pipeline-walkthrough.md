# 12 · 全流程走查：从用户输入到小说成稿

> **本文是"现在这套代码实际怎么跑"的走查说明**，不是设计意图的重述。
> 设计侧口径见 `04`（总体架构）、`05`（Agent/子代理）、`06`（数据规约）、`10`（Forge）；
> 本文按**运行时调用顺序**串起来，标注每个环节的文件与行号，供排障与改造时对照。
>
> 关键区分：**构建期（Forge）产出"应然"的圣经与细纲，生成期（Orchestrator）产出"实然"的正文与记忆。**
> 两者唯一的耦合面是磁盘上的 JSON/Markdown 文件（ADR-004/016：文件即单一事实源）。

---

## 0. 全景

```
┌─ 构建期（一次性，产出不变资产）───────────────────────────────┐
│  用户输入（一句话 brief / 已有稿子）                          │
│      │                                                       │
│      ├─ forge seed ──→ SeedSpec ──→ 初态 Blueprint            │
│      └─ forge ingest ─→ 文本切分 ──→ Blueprint               │
│              │                                               │
│      forge ask（商讨问答：槽位缺口 → 分轮提问 → 落蓝图）        │
│              │                                               │
│      forge build（递归深化 DFS：book/旁支/volume/arc/chapter）  │
│              │      每个节点 = 1 次 LLM 调用（JSON 协议）      │
│              ↓                                               │
│      Blueprint ──sync_bible（零 LLM 全量重写）──→ bible/*.json │
│                                     └─→ outline/（volumes + 细纲）│
└───────────────────────────────────────────────────────────────┘
                              ↓ 磁盘文件（唯一耦合面）
┌─ 生成期（逐章串行，ADR-002）──────────────────────────────────┐
│  produce_chapter(ws, pid, vol, ch, provider, …)                │
│      ├─ 章前：圣经注入 / 阶段判定 / JIT 补卡 / 定时事项        │
│      ├─ 事件循环：按细纲 key_events 逐事件推进（核心）          │
│      │     每事件 = 1 次生成 + 可选审校/修订/润色/编纂/视角     │
│      └─ 章后：润色 / 截断 / 拟题 / 实体更新 / 记忆回写 / 记账   │
│              ↓                                               │
│      drafts/chapters/<vol>-<ch>.md                            │
└───────────────────────────────────────────────────────────────┘
                              ↓
        promote → chapters/ → export → 全书 Markdown
```

**严格串行**：正文阶段同时至多写一章（ADR-002 修订），无并行批次。

---

## 1. 阶段一 · 立项：用户输入 → 蓝图种子

两种输入模式，都收敛到同一个中间态 **Blueprint**（`workspace/forge/blueprint.json`）。

| 模式 | 入口 | 用户输入 | 处理 |
|---|---|---|---|
| **seed** | `forge seed "<brief>"` → `forge/seed.py:run_seed` | 一句话简介 + 可选 `--volumes/--chapters` | LLM 一次调用解析成 `SeedSpec`（题材/规模/主角 hint/基调），`_seed_user_prompt`（L68）出 prompt；解析失败走 `_fallback_spec`（L127）确定性兜底 |
| **ingest** | `forge ingest <source>` → `forge/ingest.py:run_ingest`（879 行） | 已有稿子/设定文档 | 文本切分 → 逐块抽取 → 填蓝图，标记 `provenance.src="ingested"` |

Blueprint 的字段集合是**下游产出的超集**（`state.py:blank` L89）：
`meta / worldview / characters / locations / items / skills / settings / threads / style / volumes / chapters`。

**Provenance 机制**（`state.py` L163-212）——决定"后来谁能不能改我"：

| src | 含义 | 约束 |
|---|---|---|
| `user` | 用户显式回答 | **永不被模型产物覆盖**（`is_protected` L181） |
| `llm` | 模型生成 | 可被后续节点修订 |
| `template` | Genre Pack 模板 | 低置信，优先被细化 |
| `ingested` | 从已有稿子抽取 | 同上 |

- `confidence < 0.6` 且 `src=llm` 的路径进 `low_confidence_paths()`（L194）→ 递归深化时优先细化。

---

## 2. 阶段二 · 商讨问答：缺口 → 提问 → 落蓝图

模块：`forge/slots.py`（槽位表）+ `forge/ask.py`（问答）。

```
detect_gaps(bp, slots)          # 确定性，零 LLM（slots.py:145）
    → Gap{slot, reason: unfilled|low_confidence}
group_slots(gaps, per_round=4)  # 分组分轮（slots.py:187）
    → 每轮 ≤4 个槽位
run_consult(...)                # ask.py:305
    每轮：
      _build_questions()        # LLM 生成候选值（仅交互环境 need_candidates=tty）
      io.ask_free()             # 用户作答；非交互环境全取推荐值（downgraded）
      _apply_slot_value()       # 写回蓝图 + provenance(src=user,conf=1.0 / 默认 0.8)
      bp.save()                 # 轮次边界 = 保存点，中断可续
```

- **槽位 key = 蓝图点路径**（如 `meta.logline`、`characters[char:x].name`），支持 `characters[role:*]` 伪路径按角色匹配。
- **幂等**：已答槽位靠 `transcript` 里的 `ask.answer` 事件跳过（`_answered_keys` L294）。
- 留痕：每一步写 `workspace/forge/transcript.jsonl`（`append_transcript`）。

---

## 3. 阶段三 · 递归构建：Blueprint → 圣经 + 细纲

入口 `forge build` → `forge/engine.py:build`（L116）。这是**递归深化的 DFS**（ADR-018）。

### 3.1 树形

| 层 | 节点 kind | 产物 |
|---|---|---|
| L0 | `book` | 世界观/人物/文风/伏笔/卷主线一次出齐 |
| 旁支 | `worldview` → `system` → `setting_entry` | 世界设定细化 |
| 旁支 | `character_group` → `character` | 人物卡细化 |
| 旁支 | `style`、`thread_set` | 文风、伏笔集 |
| L1 | `volume`（全卷） → `arc` | `outline/volumes.json` |
| L2 | `chapter`（默认只展开第 1 卷）→ `beat` | `outline/chapters/<vol>-<ch>.md` |

父子关系 `CHILD_KIND`（nodes.py L36）、叶节点 `LEAF_KINDS`（L44）。
次序固定为 **设定 → 人物 → 文风 → 伏笔**（engine L258）——伏笔最后，可引用前面产出。

### 3.2 单节点执行（nodes.py `run_node` L960）

```python
system, user = _PROMPTS[kind](ctx)          # 按 kind 取装配函数
res = ctx.provider.complete(LLMRequest(
        messages=[system, user],
        temperature=0.5, max_tokens_out=2600,
        response_format="json_object"))     # 强制 JSON
node = _parse_node_reply(res.content)       # 宽松解析：```json 围栏 → 取首尾 {}
warns = ctx.extra_warnings + _APPLY[kind](ctx, node)   # 写回蓝图
```

**节点协议**（LLM 输出，L9）：
```json
{"artifact": {...}|null, "decide": "done|expand", "reason": "...",
 "children": [...], "open_questions": [...]}
```
`decide=expand` 时引擎按 `children` 递归展开子节点——**模型自判 + 引擎硬边界兜底**：
`max_depth=4 / max_width=4 / max_calls=60`（engine L117），解析失败 retry 1 次，仍失败回退父层产物（L191-212）。

### 3.3 确定性落盘（零 LLM）

| 函数 | 作用 |
|---|---|
| `sync_bible`（nodes.py L991） | 蓝图 → `bible/*.json` **全量重写**。剥离 `role`、补默认字段、主角引用注入 style；factions 字符串数组归一为对象数组 |
| `synthesize_worldstate` | build 末尾合成 `bible/worldstate.json`（时间原点/人物初始态） |

**落盘节奏**：`sync_bible` 在四个点被调用——骨架前置（engine L158）、book 节点后（L231）、
**每个旁支 kind 完成后**（L264）、build 末尾（L346）。即**阶段级同步而非节点级**，
中断续跑时 bible 与蓝图同 REV 不丢。

### 3.4 细纲文件格式

`outline/chapters/<vol>-<ch>.md`：front-matter + 正文，**行内双通道声明**
（nodes.py L6-7）：`key_events:` 与 `出场人物:`，生成期由 `parse_key_events` / `parse_cast_decl`
两个行内 regex 解析。

---

## 4. 阶段四 · 章节生成（核心）

入口：`orchestrator.produce_chapter`（L1025-1661）。v7 实测的调用形态见
`_harness/run_novel_v7.py:87`。

### 4.1 章前准备

| 步 | 代码 | 说明 |
|---|---|---|
| ① 读细纲 | L1082-1088 | 全文读入 `gist_text_for_events`（事件循环与 JIT 共用） |
| ② JIT 补卡 | L1094-1095 | 细纲声明出场但 bible 缺卡 → 生成前补全（防造人红线） |
| ③ 圣经注入 | L1096-1106 | `build_chapter_context` → `(system_prompt, user_goal)`，见 §5 |
| ④ 阶段判定 | L1114-1140 | `core/phase.py`：**确定性规则零 LLM**。收尾（卷剩余≤K，优先）→ 开篇（卷内章号≤N 或 established 占比<0.6）→ 行文。差异四维度：实体配额/篇幅系数(×1.3)/设定分批/回收清单 |
| ⑤ 定时事项 | L1142-1153 | `timeline.reminder_lines`：>30% 静默、≤30% 轻提示、≤10% 或到期强提示 |
| ⑥ 软拦截 | L1163-1180 | 到期 pending 未在细纲体现 → 拦截并 `tick()` 记账，连续 3 次自动 expired 放行（防死锁） |

### 4.2 事件循环（L1235-1447）

按 `parse_key_events` 拆出的声明式事件清单逐条推进（ADR-013：事件落定即回写）。
`seam = max(200, min(400, _SEAM_CHARS))`（L1219）。

**每个事件的 10 步**：

| # | 环节 | 代码 | LLM | 说明 |
|---|---|---|---|---|
| 1 | 先忆 | L1237-1241 | — | `_recall_for`（按事件文本检索记忆）+ 最近 1 章记忆固定前插 |
| 2 | 设定按需注入 | L1244-1251 | — | `settings_idx.pending_lines`：首次交代状态机，只注入"命中且未交代"的条目；开篇期按 `opening_settings_max` 分批 |
| 3 | 实体阶段注入 | L1265-1271 | — | `needs_expansion`：stage < described 的实体提示"通过行动/对白自然展开介绍" |
| 4 | RAG 检索 | L1273-1284 | ✅ | `knowledge.plan_queries`（LLM 生成查询）→ `retrieve` → 分五类渲染：`character / setting / thread / lesson / faction` |
| 5 | ADR-020 人物层 | L1286-1325 | ✅ | `match_cast`（细纲声明）+ `cast_from_text`（文本命中，≤2 人补漏）→ `render_cards`（**无条件注入**）→ `render_history_lines`（近况）→ `needs_readback`（双阈值：事件计数≥3 或故事内≥30 天）→ **`build_direction`（调度单，1 次 LLM）** |
| 6 | 世界观滚动补充 | L1336-1341 | ✅ | `supplement_settings`：事件文本出现新专有名词 → 补 settings 条目。**⚠️ 无注册表核对，是"自造设定被追认"的通道** |
| 7 | 生成 | L1345-1365 | ✅ | 普通：`_generate_with_continuation`（续写兜底）；标 `[expanded]` 的重场戏：`_generate_beats` 拆 ≤3 拍。低于 `min_event_words` 判失败 |
| 8 | 审校 + 修订 | L1369-1405 | ✅ | `reviewer.review(piece, …, scope="第 idx/total 个事件…")`，**block 才重写 1 次**；block 沉淀进 `review_lessons.json` |
| 9 | 事件级润色 | L1409-1422 | ✅ | `polish_chapter(is_chapter=False)`——润色后的文本进接缝与记忆，后续事件继承风格 |
| 10 | 拼接 + 回写 | L1424-1447 | ✅ | `strip_seam_overlap` 剥字面重叠 → `chronicler.run(max_events=2)` 抽事件 → `record_perspectives` 写角色视角 |

> **接缝只有 200–400 字**：事件 N+1 只能看到事件 N 的末尾 300 字。这是"场景重演/双版本"
> 的结构性成因之一（详见 `_harness/v7六类问题根因分析.md`）。

### 4.3 章后处理（L1448-1661）

| 顺序 | 环节 | 代码 | 说明 |
|---|---|---|---|
| 1 | 去重 | L1448 | `_dedupe_chapter_titles` |
| 2 | 落盘 | L1496-1506 | `drafts/chapters/<vol>-<ch>.md` |
| 3 | 成章润色 | L1514-1529 | `polish_chapter(is_chapter=True)`，changed 才回写 |
| 4 | 篇幅硬上限 | L1531-1544 | `length_cap_chars` 截断到**段落边界**（不腰斩句子）；在润色**之后**（润色可能加长） |
| 5 | 设定交代验证 | L1546-1558 | 扫成稿，命中关键词的未交代条目置 `revealed=true` |
| 6 | 实体进度更新 | L1560-1580 | `update_from_chapter`：四阶段 + 别名共指消歧；开篇期超配额记 `deferred` 下章优先 |
| 7 | **延迟拟题** | L1582-1592 | ADR-020 决策一：放在去重/润色/截断**全部之后**，标题描述最终正文 |
| 8 | 章级编纂兜底 | L1594-1630 | 事件循环已回写则**只合并报告、不重复编纂**；一条都没写出来才写合成事件「完成第 X 卷第 Y 章」 |
| 9 | 定时事项记账 | L1632-1643 | `timeline.tick`：命中→fired；到期未兑现→overdue 递增（≥3 warn / ≥5 软 block） |
| 10 | 审计 + 返回 | L1645-1661 | `reports/` + `audit_log`；返回 `ProductionResult` |

---

## 5. Prompt 组装全景

### 5.1 System Prompt（`context.py:build_system_prompt`）

**每章一次**，全事件复用。按序拼接：

| 序 | 段落 | 来源 |
|---|---|---|
| 1 | 角色定位 | 「你是一部{genre}长篇小说的主编剧，正在写第 X 卷第 Y 章」 |
| 2 | **本卷主线** | `volumes[vol].summary`——每章须服务它，而非只有细纲要点 |
| 3 | 世界设定 | 名称 / 概要 / **境界体系（强调不得混用「层」「重」）** / power_system.note / 铁律 rules / 文明 / 体系 / **境界波动（realm_fluctuates）** |
| 4 | 文风约束 | pov / tense / narration / tone / 目标篇幅 / **禁用词（出现即失败）** / **禁用现代词** / 专有名词固定写法 |
| 5 | **主角硬约束** | 姓名 + 性别 + 人称代词锁定（治性别漂移） |
| 6 | **人物名单** | 全员「名字（境界）」一行——**防造人红线**；细节交给事件级注入 |
| 7 | 输出纪律 | — |

> **刻意不注入**（走 RAG 事件级注入）：势力 `factions`、伏笔 `threads`、历史教训 `lessons`。
> → 这三类变成"模型生成查询 × topk 截断 × 关键词重合"的**三重概率命中**，
> 详见 `_harness/prompt作用审计.md` §二.4。
> **⚠️ 死参数**：`lessons` 形参被接收但函数体零引用（RAG 迁移残留）。

### 5.2 User Goal（`context.py:build_chapter_context`）

```
请撰写第 X 卷第 Y 章正文。
【细纲】（必须逐条落实，不得遗漏要点）   ← 剥掉首行 "# 第X章 …" 标题行后截断 ≤1200 字
【前情提要】（先忆：必须与以下已发生的事实保持连续） ← memories
要求：严格按细纲推进，写完本章全部要点，结尾必须是一个完整的收束句。
```
再追加（条件）：`【回收清单】`（收尾期）/ `【临近事项】`（定时提醒）。

### 5.3 事件级 Prompt（`orchestrator._event_goal`）

```
{章 goal}
【本步骤】只撰写本章第 idx/total 个事件：【{事件文本}】
【输出纪律】直接写正文，不要写章节标题、不要写「第X章」字样；不要重复上文已经写过的内容。
【本场人物表演指令】…（direction_lines，调度单）
【本场出场人物】…（cast_lines，人物卡·无条件注入）
【人物近况】…（hist_lines）
【前章原文回读】…（readback_text，仅事件 1）
【久未出场角色回读】…（extra_readback）
【上文接缝】…{prev_piece[-300:]}（从下面这段的结尾自然续写）
【相关前情】…（memories）
【本事件首次出现的设定】…（setting_lines）
【本事件相关人物】…        ┐
【相关知识·设定】…          │ RAG 五类
【相关知识·伏笔/教训/势力】… ┘
篇幅约 300–600 字。（最后一个事件：结尾必须是完整的收束句 / 否则：写到本事件结束即停）
```

### 5.4 其他子代理 Prompt

| 环节 | 模块 | 要点 |
|---|---|---|
| 人物调度单 | `core/director.py` | 要求性格要点来自人物卡且彼此不同 → 输出逐人表演指令 |
| 事件抽取 | `core/chronicler.py` | 输出结构化事件（参与人/受影响伏笔/状态变更），带语义双检 |
| 语义审校 | `consistency/reviewer.py` | **8 类 + 其他**（L30）：设定矛盾 / 人设漂移 / 称谓失当 / 时间线 / 战力越级 / 事实前后矛盾 / 细纲未覆盖 / 伏笔；**`scope` 参数纠正"按整章审片段"的误报** |
| 文风润色 | `core/polish.py` | 度量（AI 味打分）→ 定向改写 → 复核保底 |
| 延迟拟题 | `orchestrator._apply_chapter_title` | 按最终正文拟题，写回草稿首行 |
| 确定性规则 | `consistency/rules.py` | **不走 LLM**，9 条：`R-LEX`（现代词/现代喻体/西方典故/禁用词）、`R-PWR`（境界细分表述混用）、`R-ITEM`（物品功法一致性）、`R-CAST`（建档人物出场覆盖度）、`R-STATE`、`R-THREAD`、`R-TIME`、`R-TL`、`R-REF` |

---

## 6. 模块间信息传递（数据总线）

**所有模块间通信都过磁盘文件，没有内存态总线**（ADR-004/016）。

| 文件 | 生产者 | 消费者 | 内容 |
|---|---|---|---|
| `workspace/forge/blueprint.json` | seed/ingest/ask/build | build / sync_bible | 构建中间态 + provenance |
| `workspace/forge/transcript.jsonl` | ask/build | resume / 审计 | 问答与节点留痕 |
| `workspace/forge/nodes/*.json` | engine | resume（跳过已完成节点） | 单节点产物 |
| `bible/worldview.json` | sync_bible | `build_system_prompt` | 境界/铁律/禁用现代词/势力 |
| `bible/characters.json` | sync_bible / JIT | director / RAG / 防造人名单 | 人物卡 |
| `bible/style.json` | sync_bible | prompt / polish | pov/tense/tone/禁用词/主角 |
| `bible/plot_threads.json` | sync_bible | RAG / 回收清单 / 编纂 | 伏笔 |
| `bible/settings.json` | sync_bible / **supplement_settings** | 首次交代状态机 | 设定条目（⚠️ 可被生成期反向补充） |
| `bible/worldstate.json` | 合成 / 编纂 | timeline / R-STATE | 时间、人物当前态 |
| `bible/entity_progress.json` | `entity_tracker` | 阶段判定 / 配额 | 四阶段 + 别名 |
| `bible/review_lessons.json` | 审校 block | RAG（lesson 类） | 历史教训 |
| `outline/volumes.json` | volume 节点 | `VolumeContext` / prompt | 卷主线、chapter_range |
| `outline/chapters/<v>-<c>.md` | chapter 节点 | `parse_key_events` / `parse_cast_decl` / 审校 | 细纲 + **行内事件与出场声明** |
| `drafts/chapters/<v>-<c>.md` | produce_chapter | 回读 / 记忆 / promote | 正文草稿 |
| `chapters/<v>-<c>.md` | promote | export | 正式章节 |
| `memory/plot_events.json` | 编纂员 | 先忆 / 前情 / R-ITEM | 已落定事件（实然） |
| `memory/character_histories/*.json` | 编纂员 / 视角记忆 | `render_history_lines` | 人物经历 |
| `memory/directions/` | `director.save_direction` | 后续章调度 | 调度单留档 |
| `memory/fragment_index.json` | `harvest_fragments` | `MemoryRetriever` | 记忆碎片索引（可再生） |
| `memory/rag/vectors.json` | `MemoryIndex` | 语义检索 | 向量缓存（删了只掉性能） |
| `.index.db` / `.checksum.json` | 各写操作 | 检索加速 / 完整性校验 | 可再生缓存 |

**记忆检索分流**（`core/memory.py`）：
- `embedding.kind == "keyword"` → 精确 token 集合 + **IDF 加权余弦**（零重合即 0 分）
- 有真实 Embedding → 向量余弦
（教训：关键词模式不能用定长哈希向量，dim=256 时碰撞噪声会盖过真实信号。）

---

## 7. 输出与导出

| 命令 | 作用 |
|---|---|
| `promote` | 草稿转正：`drafts/chapters/` → `chapters/` |
| `review` | 盘后语义审校（送**尾部 3000 字**） |
| `validate` | `validate_project_full`：bible/outline/草稿一致性全检 |
| `stats` | `collect_stats`：字数、事件数、实体数 |
| `export` | `export_project` → 全书 Markdown |
| `grant` | 审批队列处置（工具权限） |
| `server` | HTTP 服务（`server.py`） |

**流水线工序**（`core/pipeline.py`）：
`立项 → 世界观 → 大纲 → 细纲 → 正文 → 审查 → 待发布 → 已完成`，带 `修订` 回流。

---

## 8. 关键不变量与当前断点

### 不变量（改代码前必读）

| 约束 | 出处 |
|---|---|
| 正文严格串行逐章，无并行批次 | ADR-002 修订 |
| bible = 应然，memory = 实然，分离互引 | ADR-011 |
| 事件落定即实时回写，不等章末 | ADR-013（事件循环即其落地） |
| SQLite 与 RAG 向量都是**可再生缓存** | ADR-016 |
| `SceneBus` 用**不可重入** `threading.Lock` | 实现约束 |
| 单元测试**绝不真调 LLM**，用 `providers/fake.py` | docs/09 §2.1 |

### 已知断点（详见两份报告）

| 断点 | 影响 | 出处 |
|---|---|---|
| 接缝仅 300 字、事件间无"已确立事实"回注 | 场景重演 / 双版本 | `_harness/v7六类问题根因分析.md` |
| `supplement_settings` 无注册表闸门 | 自造设定被追认（强化符闭环） | `_harness/prompt作用审计.md` §二.5 |
| `build_system_prompt` 的 `lessons` 死参数 | 历史教训只剩概率命中路径 | 同上 §二.1 |
| 审校类别表无「与前文重复」「新造未登记悬念」 | 重复/伏笔失控零拦截 | `consistency/reviewer.py` |
| 事件循环内不跑 `rules.py`（仅盘后） | 禁止类指令零确定性兜底 | `orchestrator.py` |
| bible 数据饥饿（26 卡中 relationships 3/26、behavior 0/26） | 人物卡过半空段、导演无米下锅 | 同上 §二.2 |
| `realm_fluctuates` 整体豁免 R-STATE | 主角境界失明 → 战力崩塌无告警 | `consistency/rules.py` |
