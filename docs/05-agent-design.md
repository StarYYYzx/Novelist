# 05 · Agent 详细设计

> 说明主角：**主编剧 Agent**（唯一常驻 Agent）、**命名子代理**，以及它们与**工具集**的协作协议。聚焦"每个 Agent 是什么、干什么、怎么交互"。

> **实现现状注记（2026-09-03，E10 收编）**
> 本文档的**职能分工与协作协议是设计意图，仍有效**；但当前实现为**编排式多决策**，并非
> 独立 LLM 会话派发：`orchestrator.produce_chapter` 在单进程内串行驱动函数/类——每个函数
> 对应本文一个角色，以**专属 prompt + 参数化上下文**近似"会话隔离"，角色间经工作区文件
> 传递事实（ADR-016，文件即事实源），无 `core/subagent.py`、无 SubagentTask 派发框架。
> §3.1/3.2 的契约与 §5 派发协议是**理想形态**，仅在 §8 演化触发条件满足时才需真会话。
> 各角色 → 代码载体对照见 §3 末表。

## 1. Agent 总体拓扑

```
                          ┌──────────────┐
       编排器(流水线驱动) ──│ 主编剧 Agent  │ ← 唯一常驻、唯一中枢
                          └──────┬───────┘
            派发/回收（isolation）
          ┌──────────────┬───────┴───────┬──────────────┐
          ▼              ▼               ▼              ▼
   世界观构建师        大纲师           文字匠          审校师
   设定维护/提取    大纲/细纲生成    正文撰写/修订     一致性/文风审查
          ▲              ▲               ▲              ▲
          └──────────────┴── 经工具读写 ──┴── 小说工作区 ─┘
```

> 拓扑中的"子代理"当前以**代码层函数/类**实现（载体对照见 §3）；隔离 = 函数参数 + 局部
> 状态，非独立会话（实现形态见 §8）。

## 2. 主编剧 Agent（Orchestrator Agent）

> **实现现状**：当前实现 = `orchestrator.produce_chapter`（orchestrator.py:1001）编排函数及其
> 调用的决策点函数链（director 调度、chronicler 编纂、title 拟题、polish 润色等）；§2.3 决策
> 空间里的"派生子代理"动作 = 调用对应函数。另有一条可选 tool-mode 路径
> （orchestrator.py:1509 用 `agent_runner.AgentRunner` 跑标准 Agent 循环，04 §5.1），
> 默认正文直出不走该循环。

### 2.1 角色定位
系统内唯一"有主人翁意识"的 Agent：理解全书目标，决定创作策略，派发并整合子代理，对全书一致性负责。

### 2.2 持久属性（由编排器注入）
| 属性 | 说明 |
| --- | --- |
| system prompt | 系统提示：角色（总编）、创作守则、可用工具说明、输出约定、安全红线 |
| goals | 本次任务的全局目标（如"推进主线第 3 卷，同时回收伏笔 V-017"） |
| budget | token / 步数 / 金钱预算（NFR-9） |
| permission profile | 工具门禁的 allow/ask/deny 配置 |
| workspace context | 可访问的项目工作区路径与当前状态摘要 |

### 2.3 循环与决策
主编剧执行标准 Agent 循环（04 §5.1）。其决策空间三类动作，由它自主选择：
1. **调用工具**：读写工作区、拉取大纲/设定引用、提交草稿等。
2. **派生子代理**：当任务偏重某一专业领域、或需要隔离并行时，派发对应子代理并把结果并入决策。
3. **返回结果**：一段工序产出的总结（产物路径 + 关键决策 + 需要人工介入的点）。

### 2.4 上下文管理策略
- 维护一份**"当前工作记忆"**：目标 + 最近一次状态摘要 + 在途任务，而非长篇正文。
- 每次行动前按需从工作区**拉取引用级信息**（标题、要点、人物 ids），正文细节写在文件里。
- 触发预算阈值时执行**压缩**（04 §5.3）。

## 3. 命名子代理

所有子代理共享同一个执行框架（Agent 循环），仅在**系统提示、权限面、输入输出契约**上不同。下表给出每个子代理的契约。

| 子代理 | 职责 | 输入（经参数） | 输出（结构化） | 权限面 |
| --- | --- | --- | --- | --- |
| **世界观构建师** | 构建/扩展设定圣经 | 一句话创意、偏好、已有圣经摘要 | 新增/修订的设定实体（人物、地点、体系、时间线） | 写 `world/`、读全书 |
| **大纲师** | 生成卷/章大纲或细纲 | 设定圣经摘要、上卷结尾、目标篇幅 | 章节大纲列表（含关键事件/转折/钩子） | 写 `outline/`、读圣经 |
| **文字匠** | 撰写正文草稿 / 按修订意见改写；整合角色演员片段 | 细纲、设定引用、文风约束、上章衔接、演员 takes | 成章 Markdown 文本（写入草稿文件） | 写 `draft/`、读 memory + Bible |
| **审校师** | 一致性 + 文风 + 逻辑语义审查 | 章节、设定引用、记忆摘要、检查清单 | 审计工单：`level(block/warn)`、对象、说明、修订建议 | 只读 |
| **伏笔监理** | 伏笔登记/回收追踪 | 新增事件清单、既有伏笔表、回收点 | 更新后的伏笔状态表 | 写 `bible/plot_threads`、读全书 |
| **检查员** | 通用自检（细纲→正文覆盖、字段完整） | 目标产出、checklist | 检查清单结果（pass/fail + 证据） | 只读 |
| **角色演员**（按需、可复数） | 以指定角色视角"试演"一节对白/反应/内心，贴合人设与近况（ADR-012） | 人物卡、本角色经历记忆摘要、场景剧本、文风约束 | `character_take` 片段（写入临时 take 目录，供文字匠整合） | 只读本人物卡 + 自己的经历记忆 + 场景；写自己 take（safe） |
| **记忆编纂员**（Chronicler） | **事件驱动**：正文每个事件落定即实时提炼剧情/人物经历/关系/伏笔/时间变化，写入记忆层并触发索引（ADR-013） | 本事件段落与上文、相关记忆/bible、编纂清单 | 记忆片段集合（写入 `memory/`）、冲突校验结果、索引更新请求 | 写 `memory/`（sensitive）、读全书 |

> 子代理是可组合的一等公民：主编剧可按需派发多次，也可新增（通过配置）而没有架构改动。**角色演员是运行时按"角色 id + 场景"动态生成**的，不独占命名席位；其"角色"由加载的人物卡 + 经历记忆决定，同一演员提示词可服务任意角色。
> 角色演员与记忆编纂员不参与整体解独立性之外的额外耦合：演员是**素材供给**，编纂是**事实沉淀**，二者都受主编剧派发与工作区规约约束。

### 3.0 实现载体对照（2026-09-03 E10 收编）

上表每个角色的当前代码载体与落地度（载体=函数/类 + 专属 prompt，非独立会话）：

| 文档角色 | 代码载体 | 落地度 |
| --- | --- | --- |
| 主编剧 | `orchestrator.produce_chapter`（orchestrator.py:1001） | 已落地 |
| 世界观构建师 | `character_enrich.py`（26 卡流水线）、`character_factory.py`、`bible.py` | 已落地 |
| 大纲师 | `forge/nodes.py` + `forge/engine.py`（ADR-017/018 递归深化） | 已落地 |
| 文字匠 | `produce_chapter` 事件循环 + `_generate_with_continuation`（:209） | 已落地 |
| 审校师 | `consistency/reviewer.py:98`（LLM 语义）+ `consistency/rules.py`（确定性 R-*） | 已落地（语义+规则双轨） |
| 伏笔监理 | `chronicler._link_threads`（chronicler.py:265） | 部分（事件→伏笔链，无独立伏笔表管理） |
| 检查员 | `forge/validate.py`、`_completeness_problems`（orchestrator.py:98） | 部分 |
| 角色演员 | `director.readback_excerpt`（director.py:418，回读素材）、`scene_tools.py`（围读会工具，仅 tool-mode 挂载） | **未落地**（ADR-012/014 生产路径缺失） |
| 记忆编纂员 | `chronicler.py` `Chronicler` 类（事件级抽取+双检+落库） | 已落地（最接近独立 agent） |

> 契约（§3.1/3.2 SubagentTask/Result）当前由**函数签名 + 参数 + 返回 dict** 近似；
> 失败语义由 `try/except` + `max_retries` 承担（无独立会话失败域）。

### 3.1 子代理输入契约（统一）

> **理想形态**：仅当 §8 演化触发条件满足、引入真会话时才需本契约为运行时 schema；
> 当前不实现派发框架，本契约仅作函数边界的对标。

```
SubagentTask {
  role: str                 # 子代理身份（决定 system prompt）
  goal: str                 # 一句话任务目标
  inputs: { ref_paths: [], spec: {} }   # 工作区文件引用（路径而非正文）
  allowed_tools: []         # 本子代理可调用工具白名单
  output_schema: object     # 期望的结构化返回契约
  max_steps, budget: int
}
```

### 3.2 子代理输出契约（统一）

> **理想形态**：当前以返回 dict（含 ok/summary/artifacts/issues）近似，`usage` 由
> 编排层 `_UsageCounter`（orchestrator.py:797）统一记账而非各会话自报。

```
SubagentResult {
  ok: bool
  summary: str              # 给主编剧的简短结论
  artifacts: [{path, hash}] # 其写入工作区的产物
  issues: [{level, object, msg, suggest}]   # 自身发现的问题（若有）
  usage: {tokens_in, tokens_out, steps, cost}
  degraded: bool            # 是否走了降级解析路径
}
```

## 4. 工具集（Tools）

工具是 Agent 与系统交互的唯一通道。每个工具按统一 schema 定义，运行时经**注册表**分派，并接受**分级门禁 + 审计**。

### 4.1 工具分类与分级

| 分类 | 工具 | 级别 | 说明 |
| --- | --- | --- | --- |
| 通用 | `read_file`, `write_file`, `list_dir`, `grep_text` | safe | 沙箱内路径，写仅限授权目录 |
| **记忆·读取** | `query_memory(essence/filters, top_k)`, `get_character_history`, `get_plot_events` | safe | RAG 检索历史经历/剧情/关系，供"写作前先忆" |
| **记忆·写入** | `append_experience`, `append_plot_event`, `record_relationship_change` | sensitive | 编纂员专用，写 `memory/`，记录版本 |
| **试演** | `write_take(char_id, chapter_id, content)` | safe | 演员写自己的试演片段到临时 take 目录 |
| **围读会** | `join_scene(scene_id)`, `say_line(scene_id, speech)`, `leave_scene(scene_id)` | safe | 演员参与受控群聊（ADR-014）；主持人经编排层调度轮次与收场 |
| 创作 | `write_draft`, `promote_draft`(转正) | safe/sensitive | 写草稿 safe；转正 sensitive |
| 设定 | `update_entity`, `add_plot_thread`, `set_timeline` | sensitive | 改设定圣经，记录版本 |
| 大纲 | `write_outline`, `patch_outline` | sensitive | 写/改大纲 |
| 一致性 | `run_rule_check`, `run_semantic_check`, `get_alerts` | safe | 触发审查 |
| **索引** | `reindex_memory` | sensitive/后台 | 编纂后重建 RAG 索引（可异步） |
| 治理 | `delete_file`, `batch_rewrite`, `checkpoint`, `publish` | **danger** | 默认 ask 门禁 |
| 观测 | `get_status`, `read_audit`, `get_budget` | safe | 查询 |

### 4.2 工具定义 schema（示例，完整接口见 07）
```
def write_draft(params: {
  project, chapter_id, content: str, draft: bool
}) -> { status, path, hash, words }

def query_memory(params: {
  query: str, filters?: {char_id?, chapter_scope?, after_vol_ch?}, top_k?: 5
}) -> { hits: [{sig, kind, text, source: {vol,ch}, score, refs: [bible_id]}] }

def write_take(params: {
  char_id, chapter_id, take_seq, content
}) -> { status, path, hash }

def join_scene(scene_id) -> { ok, active_actors, current_topic }
def say_line(scene_id, speech) -> { ok, next_turn, reactions?[] }   # 向同场景其他演员说话
def leave_scene(scene_id) -> { ok, remaining_actors }
```
- `content` 以"整章文本/片段"传入，工具负责原子写盘（临时文件 + rename）与字数统计。
- 所有写工具返回 `hash` 供一致性/审计定位版本。
- `query_memory` 为**只读检索**，返回命中片段的摘要与来源定位（卷/章），不把全文拖进上下文（控制 token，见 04§5.8）。
- `write_take` 作者为 `char_id` 指定的演员会话，写入 `workspace/takes/<ch>/<char_id>_n.md`。
- 围读会工具（ADR-014）作用域仅限**当前场景**：`say_line` 只把 `speech` 追加到该场景谈话流并广播给已 `join_scene` 的演员；`leave_scene` 后该演员不再收到后续消息。全部动作经主持人（编排层）调度与判定收敛（见 §5.3）。

### 4.3 权限门禁流程（F6.1 / ADR-007）
```
Agent 请求工具 → 注册表解析级别
  ├ safe → 执行
  ├ sensitive → 若配置 allow → 执行；否则若 ask → 挂起等人工/策略决定
  └ danger → 默认 ask；无授权拒绝
执行成功 → 写审计日志(调用者、参数摘要、结果、usage) → 返回
```

## 5. 协作协议（Workflow 级别）

### 5.1 主编剧 → 子代理 派发

> **实现现状**：本协议为设计意图。当前"派发"= 编排函数直接调用目标函数并收返回
> dict；"独立会话/沙箱约束"由函数级参数隔离 + 工作区写路径约束近似；无 AgentRun 服务。
> 编派策略（§5.2 串行优先/单次派发/人类检查点）在当前实现中原样生效。

1. 主编剧决定需要专精处理 → 构造 `SubagentTask`。
2. 由 AgentRun 服务为其创建**独立会话**，用系统提示载入对应角色的守则。
3. 子代理独立运行到收敛，返回 `SubagentResult`；其副作用（写文件）已被沙箱约束在授权目录。
4. 主编剧依据结果更新自身判断，决定下一步动作或在工作区记下阶段性结论。

### 5.2 派发策略（编派的平衡）
- **串行优先**：正文生产**严格串行、逐章**推进（用户决策，ADR-002 修订）；涉及设定因果的连续任务同样串行，保一致。
- **单次派发**：每次只派发一个任务（一章/一个事件/一次审查）；不批量并行派发多章。
- **多代理并行例外**：仅在同一章内，重场戏的多个角色演员（围读会）与独立子代理可在**隔离会话**中并行产出素材，正文整合仍由主编剧/文字匠串行完成。
- **人类检查点**：在 `turn`(里程碑)、风险动作、发布前插入人工确认/编辑点。

### 5.3 章内"演员排演"协议（ADR-012）
1. 主编剧进入某重场戏编排，判定需要多角交锋/深度对白 → 为每个出场关键角色构造演员任。
2. 主编剧分别以 `query_memory` 取各角色**近期经历摘要**，连同人物卡、场景剧本、文风约束打包进各自的 `SubagentTask`。
3. 各角色演员在**隔离会话**中独立产出 `character_take`，经 `write_take` 落到 `workspace/takes/<ch>/`。
4. 主编剧（或文字匠）把 N 个 take 合并 + 依据细纲/因果打磨，铺陈成**正文第三人称叙事**；若发现 take 间冲突或与设定相悖，则在此处仲裁（不直接改 bible）。
5. 试演片段是**临时素材**，随章节批准可归档/清理，不进入正文章节文件本身。

#### 可选变体：受控围读会（ADR-014，对应人工审查意见第 3 点）
在角色交锋激烈、需要多角互相回应的重场戏，开启**围读会**（round-table scene）代替各自独白：
1. 主编剧（主持人）创建一个 `scene_id`，注入场景说明与相关记忆，邀请 N 个演员 `join_scene`。
2. 演员轮流/按序 `say_line` 互相回应；主持人在每个 `say_line` 后广播该句给同场景其他演员，并可在轮次间注入转折或纠正"出戏"言论。
3. **结束判据（OR 条件，满足任一即收场）**：
   - **全体离场**：所有已 `join_scene` 的演员都已 `leave_scene`；
   - **达轮次上限**：`max_rounds`（如 6 轮）耗尽；
   - **话题收敛**：主持人判定已达成剧情要点/情绪基调，无需再谈；
   - **预算/超时**：达到会话 token 预算或超时；
   - **异常强制收场**：主持人或人工强制关闭场景（如越界言论、模型故障）。
4. 综述：把围读谈话纪要（`scene.transcript`）并入各演员的 `character_take`，交给文字匠整合成正文。
5. 纪律：围读会仅一场戏范围内有效（`scene_id` 隔离），参与者不可读他人未授权记忆、不可在场景外互相调用；主持人可随时收场——保证其是"受控群聊"，不回退到无界自由群聊（ADR-001）。

### 5.4 事件驱动"记忆编纂"协议（ADR-013）
1. **触发时机**：正文每个**事件落定**即由编排器派发「记忆编纂员」，而非等到章末（串行编写，见 04§4.2）。
2. 编纂员读取本事件段落 + 相关既有记忆/bible，提炼：该项事件导致的剧情进展、人物经历变化、关系变化、伏笔状态流转、时间推进。
3. 用 `append_experience` / `append_plot_event` / `record_relationship_change` 写 `memory/`，每条带来源定位（卷/章/事件）与引用到的 bible 实体 id。
4. **冲突校验**：对新增记忆执行规则 + 语义双检（与既有记忆/bible 是否矛盾），发现冲突 → 回退并提示人工仲裁，不静默入库（保真，见 09§4）。
5. 校验通过 → 触发 `reindex_memory` 增量重建 RAG 索引，让**下一事件与下一章立即可"先忆"**。
6. 编纂完成后回到主编剧，继续本章下一事件（保持"先忆—再写—实时后纂"闭环）。

### 5.5 先忆—再写—实时后纂（对上下文与记忆的衔接）
- 正文严格串行推进；每次进入下一事件/下一章的书写，Agent 优先 `query_memory` 填充"与此前事件/设定相关的历史"，而非依赖会话内已过期的记忆。
- 事件一旦落定即实时编纂入库（§5.4），故同一章内后续事件跨章节一致性不依赖模型窗口大小，而依赖记忆层保真 + 检索命中（ADR-011 的本意）。

## 6. 状态与进度表示

- 当前所处工序（流水线状态机，见 04/06）由编排器维护。
- Agent 的"当前在想什么/做到哪"，以事件流实时对外暴露（交互层展示）。
- 一组 `agent_status` 事件：`planning / running_tool / spawn_subagent / awaiting_human / finished`。

## 7. 边界与失败语义
- 主编剧是唯一"长寿命"实体；子代理失败不伤全局，只重试或重派该任务。
- 任何 Agent 达到 `max_steps`/`budget` 上限 → 强制收敛并以当前状态返回，不阻塞不产生脏副作用。
- 子代理之间不得互相调用（避免不可控网状通信，ADR-001）。
- **演员隔离**：角色演员只可读本人物卡、自己经历、授权场景记忆；写工具仅限自己的 take。（详见 04§5.9、07 工具权限面。）
- **编纂保真**：记忆编纂员写入需经冲突校验；校验失败回退并提示人工，不静默污染记忆（见 09§4）。
- 记忆写入（sensitive）与索引重建可在**流程中异步进行**，但其冲突校验结果必须可见、可回滚（配合检查点，见 04§5.6）。

## 8. 编排式多决策（当前实现）与可选演化路径

> 本节是 E10 收编结论（2026-09-03）：docs/05 全篇职能设计仍有效，但**执行载体已收敛为
> 编排式多决策**；何时该退回"真会话派发"由下面触发条件决定，不提前造框架。

### 8.1 实现形态（现状）
- 单进程串行：`produce_chapter`（orchestrator.py:1001）为中枢，按工序调用各"职能函数"，
  每函数 = 专属 system prompt/输入装配的一次 LLM 调用或确定性处理（载体对照见 §3.0）。
- 角色间事实传递一律经**工作区文件**（ADR-016：文件是事实源；索引/账本为可再生投影），
  不依赖会话内共享上下文。
- 一致性由**确定性层**吸收：账本（rel_ledger）、实体状态机、worldstate、R-* 规则扫正文、
  事件冲突双检——模型侧一致性短板由机器补齐，会话隔离的边际收益被稀释。
- 预算记账统一走 `_UsageCounter`（orchestrator.py:797）；失败语义 = `try/except` + `max_retries`。

### 8.2 与设计意图的差距（真会话才能买到的，当前没有）
1. **独立偏置视角**：审校在生成同窗口内自审有确认偏置，无"第三方冷启动审校"。
2. 独立会话 usage/压缩策略（当前单计数器粗粒度）。
3. 真并行（ADR-002 已拍板正文严格串行，此条仅剩"同章多角色 takes"场景有意义）。

### 8.3 演化触发条件（满足才引入真会话，单点补不整体造）
- **A·审校独立会话**：真机 ch7+ 暴露"同会话自审发现不了系统性偏误"（AI 味、重复句式），
  且 R-* 规则 + polish 已尽力仍压不住 → 补一条：审校师新会话（独立偏置 + 低温 + 读全文 +
  规则告警输入），产出 ReviewIssue 列表回编排层。
- **B·角色演员批量 takes**：重场戏多角色对白被证伪/单薄，围读会（ADR-014）能改善 → 此时
  `scene_tools.py` 已备工具，只需补真会话调度层（见 §5.3/可选变体）。
- 其余场景（上下文隔离、失败域、产品化多角色 UI/预算墙）暂不构成引入理由。

### 8.4 明示未落地项（勿按本文档 §3/§5 误以为已实现）
- 角色演员 `character_take` 生产路径：仅 `readback_excerpt`（回读素材）有实现，takes 落地/整合未接。
- 围读会工具（`scene_tools.py`）仅 tool-mode 挂载，无生产触发路径。
- 伏笔监理独立伏笔表管理（当前只有 chronicler 事件→伏笔链）。
