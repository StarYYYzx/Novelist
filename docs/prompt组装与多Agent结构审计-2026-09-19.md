# Prompt 组装与多 Agent 结构审计（2026-09-19）

> 审计对象：**34 个 LLM 交互点**（`provider.complete` / `llm.complete` 全枚举）的输入侧信息完整性，
> 以及引入常驻对话 Agent（ADR-036 / docs/08 M3ac）后的多 Agent 结构与工具面。
> 与既有报告的分工：`prompt审计-2026-09-12`（结构/成本）、`prompt组装结构审计-2026-09-12`
> （信息覆盖，8 项已于批次 2 修复）——本报告**先复验那 8 项的现状**，再报**新发现的缺口**。
> 方法：静态读码 + 逐点行号抽查复核（本报告所有 P1 结论均经二次人工核对）。
> 数据基线：src 91 文件 31858 行，1132 passed（提交 `495b218` 时点）。

---

## 第一部分：Prompt 组装总检

### 1.0 复验：09-12 批次 2 的 8 项修复**全部仍成立**

| 项 | 现状 | 依据 |
|---|---|---|
| 审校章级窗口 head+tail | ✅ 12000/3000（`reviewer.py:160`，`CHAPTER_REVIEW_CHARS`） | 事件级仍 tail 3000（设计如此） |
| 审校细纲窗口与生成侧同源 | ✅ `gist[:GIST_MAX_CHARS]`（2400 共享常量） | `reviewer.py:176` |
| REVIEW_PROMPT 维度 ↔ CATEGORIES | ✅ 对齐，且有 `CATEGORY_ALIASES` 别名收敛 | `reviewer.py:39-48` |
| `_bible_brief` 补 glossary/craft/realm_fluctuates/modern | ✅ 四项都在 | `reviewer.py:112-137` |
| 检索规划窗口 1200/400 head_tail | ✅ | `knowledge.py:40-41` |
| 设定补充窗口 head+tail | ✅ 现为 **1600/600**（报告写 2000/600，数值后调过，语义不变） | `orchestrator.py:672-673` |
| 细纲层【世界观基座】 | ✅ 仍在，`_worldview_block` 共享 | `forge/nodes.py:580-581` |
| 正文 system prompt 世界观全量 | ✅ levels/rules/civilizations/systems/realm_fluctuates/modern_words | `context.py:245-268` |

**结论：09-12 修复没有回退。下面的缺口全是新发现。**

### 1.1 章节生成主链：新缺口

| # | 级别 | 缺口 | 证据 | 修法 |
|---|---|---|---|---|
| C-P1-1 | **P1** | **拍展开/微节拍丢整个人物层**：`_generate_beats` / `_generate_microbeats` 只注入 goal + 拍清单 + readback + memories + related，**无** cast_lines（人物卡）/ direction_lines（导演调度）/ live_state（实然状态钉死）/ line_cards（线索卡）/ prose_window（正文滑窗）/ banned（点名禁令）。重场戏（节拍展开的触发场景）恰是最需要人设与状态钉死的地方，却比单事件路径信息更少 | `orchestrator.py:756-810, 823-895`；对照 `_event_goal:954` 的 18 类块 | beats 调用方把 `_event_goal` 已组好的块**透传**下去（拆包 `goal` 为结构化参数，或在 beats 内复用同一装配函数） |
| C-P1-2 | **P1** | **审校看不到实然状态与实然时间**：`_bible_brief` 只读 bible 静态卡；「战力越级 / 事实矛盾 / 时间线」三个维度判定时没有 worldstate 硬状态、没有"今天是第几天"。且章级 `review_chapter_file` 连 `memories` 都不传（前情恒为"（无前情）"） | `reviewer.py:200-215, 234-243`；`_bible_brief:103-149` | `review_context` 注入 worldstate 摘要块 + 实然时间行；`review_chapter_file` 传 memories（复用事件级调用方的记忆检索） |
| C-P1-3 | **P1** | **视角记录窗口 tail-only**：`record_perspectives` 只看 `text[-2500:]`。`extract` 已因"tail-only 漏章头时间行"修过同一坑（`_chapter_window` head+tail），视角记录没跟上——事件前半出场者的视角/关系变化漏记，直接污染 relations 账本 | `chronicler.py:699`；对照 `:111` `_chapter_window` | 复用 `_chapter_window`（或 head+tail 2500/800） |
| C-P1-4 | **P1** | **导演层世界观基座不全**：`worldview_base_lines` 只有 name/summary/levels/rules[:6]，**无 civilizations / systems / realm_fluctuates / modern_words**。正文 system prompt 已修全量注入（09-12 批次 2），S-2 导演层没同步——导演仍可产出越界调度指令 | `context.py:76-97`；`director.py:339` | `worldview_base_lines` 加可选 `full=True` 参数（导演层用全量，细纲/卷纲层维持紧凑），或拆 `worldview_director_lines` |
| C-P2-1 | P2 | **review_lessons 可见面过窄**：仅生成侧 RAG（`knowledge.py:131` → related.lesson）可见；审校/导演/广播/润色/编纂都看不到——审校对"已教训过的同类问题"无去重依据 | `reviewer.py` REVIEW_PROMPT 无 lessons 块 | 审校 prompt 注入本章相关 lessons 前 5 条（复用知识层检索） |
| C-P2-2 | P2 | **covenant 承诺账本生成侧零注入**：只在 forge roll-window 门控与 CLI 展示用；正文/细纲生成看不到"哪些承诺不得触碰"（lines 账本与卷主线只覆盖了其中一部分承诺） | grep 全库：24 个生成链调用点无一引用 covenant | `build_chapter_context` 注入本卷承诺摘要（只读，一句话级） |
| C-P2-3 | P2 | **实然时间锚只在广播可见**（`broadcast.py:468-469`）；`_event_goal` 无"今天是第几天"（只有 reminder_lines），审校判「时间线」维度无时间基线 | `broadcast.py:468` vs `orchestrator.py:954-1064` | `_event_goal` 与审校各加一行实然时间锚（worldstate.time） |
| C-P2-4 | P2 | **事件级润色无 global_context**：禁令/前卷事实/前章尾只在章级润色注入，事件级（event_polish）透传 system 但无全局上下文 | `orchestrator.py:2220-2225` vs `:2474-2486` | 事件级润色传入精简 global_context（禁令 + 前章尾 180 字） |

**确认做对了（不要动）**：34 个调用点**全部**声明了输出格式契约（JSON schema / 行式 / 字数段）；
`_UsageCounter.complete`（orchestrator:1326）是纯透传记账包装，不组装内容，无问题；
事件级 `_event_goal` 仍是全链信息最厚的点（18 类块 + 分层淘汰）。

### 1.2 Forge 构建链：新缺口

| # | 级别 | 缺口 | 证据 | 修法 |
|---|---|---|---|---|
| F-P1-1 | **P1** | **蓝图评审 findings 不进构建链 prompt**：`run_blueprint_review` 的高危矛盾/撞型结论落盘后，只有正文侧消费（`orchestrator.py:1831-1834 load_blueprint_bans`）；forge 的 volume/chapter 节点一律看不到——评审白跑半边 | `forge/coherence.py:220-281`；`forge/engine.py:476-489` 只落盘 | `_volume_prompt`/`_chapter_prompt` 注入 findings 禁令块（复用 `load_blueprint_bans`） |
| F-P1-2 | **P1** | **review_lessons 构建链零可见**：与 C-P2-1 同根——已审校过的错在**构建期**重复犯（细纲层就种下矛盾，下游审校再抓一次） | 全 forge/ 无 lessons 引用 | chapter/volume 节点注入"全书教训 Top-N"（按类别聚合，非全文） |
| F-P1-3 | **P1** | **build 链 vol≥2 卷纲无承上信息**：`_volume_prompt` 只读本卷 book 规划；前卷已定稿的 arc(goal/obstacle/outcome/bridge)/chapter_notes 不进 prompt。**只有 roll 链有** `roll_context` 四块（`engine.py:788-834`）——初建多卷时卷 2+ 是半盲排 | `forge/nodes.py:341-403`（`plan` 只取本卷，`:346-347`） | 初建链补"前卷已定稿摘要"块（复用 `_roll_context` 的数据源） |
| F-P1-4 | **P1** | **ingest LLM 抽取无 genre 上下文**：`_llm_extract` 只给 system + 正文尾 2500 字；realm/locations/items 无类型包对照表（确定性路径才用 `pack.extract_lexicon`）——从已有稿子抽境界时无表可对 | `forge/ingest.py:417-424`；对照 `:763-768` | LLM 抽取 prompt 注入 pack.extract_lexicon 词表与境界表 |
| F-P1-5 | **P1** | **conflicts / covenant 纯事后工具，不进任何构建 prompt**：冲突挂起记录只在收尾日志与 shell 裁决可见；covenant 只用于 roll-window 触碰判定。重跑/roll 时模型不知道"这里有未裁决冲突/有承诺不能碰" | `forge/engine.py:629-646, 1270-1329` | volume/chapter 节点注入"待裁决冲突清单（id+summary）"与本卷 covenant 摘要 |
| F-P1-6 | **P1** | **arc 节点承上启下断档**：只给卷主线的 title/summary/key_beats，**不给**卷级 arc(goal/obstacle/outcome/bridge) 与 chapter_notes——弧层看不到"本卷要达成什么、付出什么代价" | `forge/nodes.py:871-884`（`:877` 只取三键） | 把 plan 的 arc/chapter_notes 一并注入（一行改动） |
| F-P2-1 | P2 | **seed 选型盲**：只见类型包 id 清单，不见各包内容（境界体系/金手指范式/节奏）——"选哪个模板"是无依据选择 | `forge/seed.py:70-73` | 注入各包的 3 行摘要（name + levels[:3] + mechanic 一句话） |
| F-P2-2 | P2 | **character 节点与 character_factory 信息不对称**：forge 侧要求 `relationships.target 必须是已有角色 id`、`power.faction`，却**不给**现有角色 id 名册与势力名册；factory 侧（正文期）反而都有。两边关系锚定各缺一半 | `forge/nodes.py:762-782` vs `core/character_factory.py:256-271` | `_character_prompt` 注入现有角色 id 名册 + factions 名册（与 factory 同源） |
| F-P2-3 | P2 | **system / setting_entry 节点无境界表/铁律**：写力量维度条目靠猜 | `forge/nodes.py:715-737` | 这两个 kind 也注入 `_worldview_block` |
| F-P2-4 | P2 | **revise_book_section 修订可悄悄背离拍板项**：只带兄弟要点 + 当前模块全文 + 用户建议，**无 anchors / endgame / pace / romance** | `forge/nodes.py:1913-1948` | 注入 meta 拍板项块 |
| F-P2-5 | P2 | **coherence 连读 prompt 声明与实据不符**：prompt 头自称输入含"张力/钩子"，实际 `gather_chapter_plans` 只取 title + key_events | `forge/coherence.py:44` vs `:67-71` | gather 补 `tension`/`hook` 字段，或改 prompt 头去掉该宣称 |
| F-P2-6 | P2 | **世界观基座截断偏紧**：rules[:6] / glossary[:12] / banned[:20]，大世界观尾部规则静默丢失（无告警） | `context.py:95`、`forge/nodes.py:843-846` | 超限时 emit 一条 warn（先可见，再决定是否放宽） |
| F-P2-7 | P2 | **`_sibling_block` 定义后零调用**（兄弟摘要机制名存实亡） | `forge/nodes.py:669` | 接入 arc/beat 节点，或删除（二选一，勿留死机制） |

---

## 第二部分：引入常驻主 Agent 后的结构与工具审查

> 对照 ADR-036 设计（console 自然语通道 + 三级门禁 + 结构化查询工具 + 会话持久化）。
> 结论：**拓扑不用改**——对话态主编剧复用 `AgentRunner` 是对的（与批式编排共享循环原语，
> docs/05 §2.5 已写清两者分工）。要补的是 **AgentRunner 的会话化能力缺口**与**工具面的三个洞**。

### 2.1 AgentRunner 的会话化缺口（M3ac 实现前必须补）

| # | 缺口 | 证据 | 修法 |
|---|---|---|---|
| S-1 | **无公开的消息历史注入/导出 API**：`_messages` 私有，只有 `system()` 一个追加口。会话持久化（M3ac-3）需要"启动时回放 jsonl → 恢复对话" | `agent_runner.py:107` | 加 `load_messages(msgs)` / `export_messages()` 两个方法（浅拷贝防御） |
| S-2 | **轮次提示语是子代理口吻**：run_evidence 每轮注入"第 i/N 轮：基于上一步的观察继续决定（读证据或输出结论）"——对话场景里用户会看到模型被当成取证子代理催 | `agent_runner.py:236-238` | 加 `mode="evidence"|"chat"` 参数：chat 模式下轮次提示改为中性（"继续"），goal 注入文案改为对话语气 |
| S-3 | **无 streaming**：编码 agent 体验核心（边生成边看）缺失；`LLMRequest.streaming` 字段存在但 provider 侧未实现流式 | `core/llm.py`（`streaming=False` 全局）；`providers/openai.py` 无 stream 路径 | 第二版做（SSE 解析 + console 增量渲染）；第一版用"思考中…"占位即可 |
| S-4 | 审批 UX **已就绪**（无需新造）：`_interactive_decision`（cli.py）已在 `chapter --loop` 实战接线，console 侧包一层 FilterableIO 版本即可 | `cli.py _interactive_decision` | 复用，仅适配 console 的 IO 通道 |
| S-5 | **成本护栏默认松**：`enforce_cost=False`（只记账不硬停）。对话 agent 自主多轮调用，无硬顶就有烧钱循环风险 | `agent_runner.py:104` | 对话态默认 `enforce_cost=True` + `max_cost`（如 ¥0.5/会话，可配置进 `[generation]`） |
| S-6 | **两套成本账本分裂**：批式走 `_UsageCounter.complete`（orchestrator:1326），对话态走 `AgentRunner._account`——`stats` 汇报只覆盖前者，对话开销不可见 | `orchestrator.py:1326` vs `agent_runner.py:144-167` | `/agent status` 报本会话账；跨会话汇总落 session.jsonl（每段结尾写 usage 行） |

### 2.2 工具面：M3ac-2 之外新发现的三个洞

| # | 缺口 | 修法 |
|---|---|---|
| T-1 | M3ac-2 规划的 4 个查询工具（list_chapters/get_bible/get_outline/list_conflicts）**应同时登记进 `EVIDENCE_TOOL_NAMES`**——chronicler/reviewer 证据环（ADR-032）现在只能靠 read_file 猜 JSON 结构，同样受"文件布局幻觉"之苦 | 注册时双写白名单；注意 `list_conflicts` 对证据环也是只读安全的 |
| T-2 | **缺 worldstate / 实然查询工具**：对话态最高频问题（"叶蓝现在什么境界""现在是第几天"）只能 read_file 猜路径。与第一部分 C-P1-2 / C-P2-3 同根——**实然状态的程序化访问全系统缺位** | `get_worldstate(character_id=None)`：无参返回时间+全体硬状态摘要，有参返回单人卡 |
| T-3 | **冲突裁决是函数不是工具**：`resolve_conflict` 只能 CLI/console 键盘命令触发。对话态用户说"那条伏笔冲突就保留旧的吧"，agent 无法代办——要么用户切回 `/resolve`，要么放第二版把 `resolve_conflict` 包成 **danger 级工具**（正好当三级门禁的实战试金石） | 第二版；第一版让 agent 提示用户用 `/resolve` |

### 2.3 结构纪律（写进实现时的护栏）

- **写路径纪律**：对话态 agent 的写工具只有 `write_draft`（进 `drafts/`）与 `write_file`（项目内，sensitive）。
  **promote/publish 维持 danger**——正文转正必须走 review → promote 流水线，对话态不得抄近路。
  这条不进代码也行（现有分级已是如此），但要写进主编剧 system prompt 的边界段。
- ** spawn 缺席是刻意的**：第一版对话态不派生子代理（拍板 C 的工具面最小化）；
  第二版若要做"把这段交给审校师看看"，复用 `run_evidence` + evidence registry 即可，不新造。
- **profile 分流**：对话态用全量 registry（`build_registry`），证据环继续用
  `evidence_registry`——两套白名单别混（`EVIDENCE_TOOL_NAMES` 是只读语义的最后防线）。

---

## 第三部分：建议执行顺序

**批次 3（prompt 缺口，按"改上游优先"排，与批次 D 真机验收合并跑对比）：**

1. **C-P1-1** beats 人物层透传（影响面最大：重场戏质量）
2. **C-P1-4 + C-P1-2** 实然状态/时间进审校与导演基座补全（同一条信息链：worldstate 的程序化注入面）
3. **F-P1-3 + F-P1-6** 构建链承上启下（vol≥2 半盲排 + arc 断档，同一次改动）
4. **F-P1-1 + F-P1-2** findings/lessons 进构建链（同一注入手法）
5. **C-P1-3** 视角记录窗口（一行级）
6. **F-P1-4 / F-P1-5** ingest genre 上下文 + conflicts/covenant 注入
7. P2 批（F-P2-1/2/3/4/5 都是小注入；F-P2-6 先加告警；F-P2-7 二选一）

**M3ac 实现清单修正**（相对 docs/08 现版增补）：

- M3ac-1 追加：AgentRunner `load_messages/export_messages` + `mode="chat"`（S-1/S-2）
- M3ac-2 追加：`get_worldstate`（T-2）+ 查询工具双写 EVIDENCE_TOOL_NAMES（T-1）
- M3ac-3 追加：`enforce_cost=True` + max_cost 默认（S-5）、会话段尾写 usage（S-6）
- streaming（S-3）与 conflict 裁决工具（T-3）显式划入**第二版**

---

## 附：本次枚举的 34 个调用点

章节主链 24 个（orchestrator 11 / polish 1 / reviewer 1 / knowledge 1 / chronicler 2 /
director 1 / broadcast 1 / character_factory 2 / character_enrich 1 / volume_facts 1 /
bible_feedback 1 / agent_runner 2）；Forge 链 9 个（seed 1 / nodes 2 / ask 2 /
coherence 2 / ingest 1 / character 双套另计）；provider 层 1 个（deepseek 重试封装，非 prompt 点）。
