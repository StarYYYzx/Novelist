# 03 · 多 Agent 架构选型与决策记录

> 本文档是**最重要的决策文档**：回答"为什么选这个架构"。包含候选模式对比、按本项目需求的加权评估、最终结论，以及一组可追溯的架构决策记录（ADR）。

## 1. 候选多 Agent 架构模式

业界与多 Agent 框架（AutoGen、LangGraph、CrewAI、Claude Code 等）中常见的组织模式可归纳为四类：

### M1 编排者—工作者（Orchestrator-Workers）
一个**常驻主 Agent**（超级员/总编）负责理解全局目标、拆解任务、**按需派生**子代理并把结果回收整合。
- 代表：Claude Code 的主 Agent + Subagents、LangGraph 的 Supervisor 模式。
- 优点：全局一致性最强、控制流清晰、可干预点明确。
- 缺点：主 Agent 是决策中枢，可能成为吞吐瓶颈；主 Agent 上下文需精心管理。

### M2 流水线（Pipeline / Chain）
把创作过程固化为固定工序，每个工序由专责 Agent 串行（或部分并行）执行。
- 代表：LangChain 的 Chain、CrewAI 的顺序流程。
- 优点：简单、可预测、易于评测与并行。
- 缺点：灵活性低，难以应对自发跳步/回流；工序间信息交接靠显式传参。

### M3 黑板（Blackboard）
共享一块可变的状态存储（黑板），多个独立 Agent 通过读写黑板协作，黑板 + 控制机制驱动协作。
- 代表：经典黑板系统、Multi-Agent"共享记忆 / 共享文件"。
- 优点：天然契合共享设定/大纲的协作语义，状态显式可审。
- 缺点：若无健壮的仲裁，容易各自为政、相互覆盖；需要明确的"谁有写权限"约定。

### M4 群体自治（Peer-to-Peer / Swarm / 辩论）
多个对等 Agent 自由通信、辩论、协作完成目标（如辩论式、多角色扮演）。
- 代表：AutoGen 的群聊模式、ChatDev 的多人协作。
- 优点：发散性强，适合头脑风暴、多方案冲突。
- 缺点：**不可控、不可预测、成本高**，长文本下极易上下文爆炸与漂移——对"要书质量与一致性"的严肃写作不友好。

## 2. 与本项目需求的映射

回到需求（02）：本项目最关键的五点诉求是——
1. **长文本全局一致性**（G2/NFR-4）——需要强仲裁、集中控制。
2. **工序严格且可回流**（F1）——需要流水线式状态机。
3. **共享设定圣经**（F2）——需要显式共享状态（黑板）。
4. **Claude Code 式 Agent 形态**（约束）——需要 Agent 循环 + 工具 + 按需子代理。
5. **串行逐章 + 事件实时回写 + 人工介入**（F3、F11、UC-10）——需要可暂停、可审批、严格串行的流程编排。

## 3. 加权评估

按本项目特征打分（1–5 分，权重从需求导出）：

| 评估维度 | 权重 | M1 编排者 | M2 流水线 | M3 黑板 | M4 群体自治 |
| --- | --- | --- | --- | --- | --- |
| 全局一致性 | 25% | **5** | 3 | 4 | 1 |
| 流程可控可回流 | 20% | **5** | 4 | 3 | 2 |
| 扩展/可观测性 | 15% | **4** | 3 | 4 | 2 |
| 人工介入友好 | 15% | **5** | 3 | 4 | 2 |
| 突发/创造性 | 10% | 3 | 2 | 3 | **5** |
| 实现复杂度/可控风险 | 15% | 3 | **5** | 3 | 2 |
| **加权总分** | | **4.45** | 3.40 | 3.55 | 2.05 |

## 4. 结论：采用混合架构

> **主模式 M1（编排者—工作者）+ 落地 M3（黑板 → 小说工作区）+ 封装 M2（流水线状态机）**，
> 内核复用 Claude Code 式 **Agent 循环 + 工具 + 按需子代理**。

三者互补，各司其职：

- **M1 为主**：总编（主编剧）Agent 是唯一中枢，拥有对工序推进、子代理派发与结果仲裁的决定权，保证全局一致与可控。
- **M3 为底**：把抽象"黑板"落地为**小说工作区**——一套文件系统目录（设定圣经、大纲、正文、审查记录）。文件即共享记忆，Agent 之间不传长篇正文，只通过读写文件交换信息，从根本上规避上下文爆炸。写权限由工作区规约 + 工具协议约束。
- **M2 为壳**：工序本身是一个**流水线状态机**（大纲→细纲→正文→审查→修订），供编排层驱动与人工跟踪，同时保留自由回流路径（修订、伏笔逾期、人工跳步）。**正文严格串行逐章推进**，不做批次并行。

形成架构图语言：**调度层驱动流水线 → 主编剧经 Agent 循环调工具 → 工具读写小说工作区 → 需要专精时派生子代理（含"角色演员"）→ 子代理也通过工具与工作区交互 → 正文严格串行逐章 → 每完成一个事件即由"记忆编纂"Agent 实时把剧情/人物/关系的演进回写记忆层，供下一事件立即可"先忆"（ADR-011/012/013）。**

> 记忆在此构架中是**三态活水**：bible（权威约束·应然）与 **memory（事实演进·实然，支持 RAG 检索）** 分离，中间由**事件驱动编纂**持续同步；角色生动性由**演员 Agent** 按需供给。

## 5. 架构决策记录（ADR）

以下 ADR 是已确认的决策；每条状态均为 **Accepted**，可由后续文档细化或修订（修订需更新本文档并记录原因）。

### ADR-001 系统内核形态
- **决策**：采用 Claude Code 式 **单主 Agent 循环 + 工具调用 + 按需子代理**，而不是**无界的**多 Agent 自由群聊。
- **理由**：长文本场景需要强可控与强一致性（见第 1—3 节评估）。
- **影响**：Agent 循环、工具注册/权限、子代理协议成为核心基础设施。
- **修订（本轮评审修订）**：本决策禁止的是**无编排者、无纪律的自由群聊**。为增强"扮演排演"的生动性，允许一种**主持人驱动的受控群聊（角色围读会）**作为显式例外——演员以 `join_scene/say_line/leave_scene` 工具在主编剧（主持人）调度下交锋，并由明确判据收敛结束（见 ADR-014）。该例外仍服从主编剧中枢与工具门禁，不推翻本决策的"强控制"原则。

### ADR-002 模式组合
- **决策**：M1 + M3 + M2 混合（编排者 + 黑板/工作区 + 流水线），不采用单一模式。
- **理由**：单一模式无法同时满足一致、可控、可回流、可共享四点。
- **影响**：总体架构含三类组件——编排（流水线调度）、Agent 层（主编剧+子代理）、存储层（小说工作区）。
- **修订（本轮串行化改造）**：正文生产**严格串行、逐章推进**，**彻底移除并行批次机制**（批基线/`pending_release`/批次门禁）。每完成一个事件即实时回写记录（见 ADR-013）。若未来需并行加速，作为 Backlog 另立决策（见 02§6）。

### ADR-003 LLM 可插拔
- **决策**：定义统一 **LLM Provider 适配器接口**，云 API 与本地部署皆为插件。
- **理由**：需求确认"可插拔，云/本地均支持"；避免厂商锁定；成本与隐私灵活。
- **影响**：核心不直接依赖任何 SDK；能力降级语义需明确（如某模型不支持工具调用时回退）。
- **已否决**：直接绑定某一家厂商 SDK。

### ADR-004 长期记忆落地为文件系统工作区
- **决策**：跨 Agent 信息交换通过**小说工作区（文件系统 + 契约化 JSON/Markdown）**，而不是在对话上下文里传全文。
- **理由**：规避上下文大小限制；文件可版本化（Git）、可人工编辑、可检查点备份。
- **影响**：数据设计（06）定义目录规约与 Schema；工具集内含读写文件的能力。

### ADR-005 主 Agent 上下文预算
- **决策**：主 Agent 的会话上下文实行**预算与压缩**，超阈值时把细节下沉到工作区文件、引用而非搬运。
- **理由**：长连载生命周期极长，主上下文若不管理将必然爆炸。
- **影响**：编排层内置"摘要/归档/引用"机制（见 04 关键机制）。

### ADR-006 结构化输出契约
- **决策**：Agent 产出尽量以**契约化 JSON** 返回（定义 Schema），失败自动重试/降级。
- **理由**：长文 + 自动流水线需要可解析、可校验、可返工的输出。
- **影响**：接口设计（07）给出 JSON Schema 约定与重试策略。

### ADR-007 权限门禁
- **决策**：危险操作（删除、批量改写、覆盖设定、发布）默认 `ask`，可配置 `allow/deny/ask`。
- **理由**：自动化但保留人对不可逆操作的控制权（参考 Claude Code 权限模型）。
- **影响**：工具系统分"安全/敏感/危险"三级；危险级走门禁。

### ADR-008 工序抽象为流水线状态机
- **决策**：`立项 → 世界观 → 大纲 → 细纲 → 正文 → 审查 → 待发布` + `修订` 回流边。
- **理由**：让工序可控、可中断、可观测，同时保留回流自由度；正文阶段严格串行推进。
- **影响**：需要一个轻量状态机模块与检查点机制。

### ADR-009 一致性双检
- **决策**：一致性保障 = **确定性规则检 + LLM 语义检**双层。
- **理由**：规则检保证零失败基线，LLM 检覆盖语义层，二者互补、成本可控。
- **影响**：审查子代理 + 规则引擎并存（见 05/09）。

### ADR-010 存储与版本
- **决策**：正文与设定以**可 Git 化的文本文件**存储；另建项目级 JSON 快照作为检查点。
- **理由**：人力可读、可 diff、可备份，检查点保证程序级恢复。
- **影响**：数据设计（06）明确双轨存储。

### ADR-011 记忆子系统（Memory + RAG）
- **决策**：在"设定圣经（权威约束）"之外，建立**记忆子系统**——将剧情演进、人物经历、关系变化沉淀为**半结构化的记忆片段**，并提供 **RAG 检索**（语义 + 关键词）供全书各 Agent 按需召回；记忆与 bible 分离管理。
- **理由**：长连载 AI 会"遗忘"早期事件与人物历程（用户核心痛点）；bible 描述"人设应当如何"（应然），memory 记录"发生了什么、角色经历了什么"（实然），两者必须区分，写作时才既能守规矩又能忆旧事。
- **影响**：新增记忆层数据结构（06）、记忆检索/写入接口与工具（07）、编纂 Agent（05）。memory 不取代 bible，二者并存、互相引用。

### ADR-012 演员 Agent（Character Actor）
- **决策**：在**章内情节编写**阶段，按需派生**以指定角色人设 + 近期经历 + 场景刺激为上下文的"演员 Agent"**，产出该角色视角的**试演片段**（对白/反应/内心），再由文字匠整合为正文。
- **理由**：让角色说话更有"人味儿"、更贴合人设与已有经历（用户"像演员演戏"的诉求）；比让单一文字匠凭空揣测角色更生动。
- **影响**：新增运行时可动态生成的角色（非固定命名常驻）、一个临时"试演片段"产物类型、actor 的工具视图收敛（只读本人物卡 + 自己经历 + 场景，写自己的 take）。**保持可控**：演员不写正文、不改 bible、不能读他人未授权记忆，只是素材供给者（见 05/06）。

### ADR-013 记忆编纂·事件驱动实时回写（Chronicler / Keeper）
- **决策**：正文**严格串行逐章**推进；在创作过程中，**每落定一个事件**即由**记忆编纂员 Agent** 实时把"该事件导致的人物经历、剧情进展、关系变化、伏笔流转、时间推进"提炼回写为结构化记忆，经一致性校验后更新 RAG 索引——**不等章末、不积压**。
- **理由**：让记忆"由权威角色按规约实时沉淀"，串行编写下事件一旦发生即固化，保证后续事件与下一章立即可"先忆"（解决"AI 遗忘"）；同时避免批次/章末批量回写带来的滞后与上下文漂移。
- **影响**：正文主时序中每个事件落点触发一次编纂回写；编纂拥有 sensitive 级写记忆工具；对新增记忆做"与既有记忆/bible 冲突"校验（见 06§4.4）；删除"章节收尾统一编纂"工序与"并行批次基线"机制（彻底移除并行，见 ADR-002 修订）。

### ADR-014 受控围读会（主持人驱动的演员群聊）
- **决策**：允许在**重场戏"剧本围读"**阶段让多个演员 Agent 在**主持人（主编剧）调度下的受控群聊**中交锋——演员经 `join_scene/say_line/leave_scene` 工具参与，主持人负责调度轮次、注入场景/记忆并判定结束；群聊有**明确的收敛判据**，不无限进行。
- **理由**：演员间"对一下戏"比让每个演员各自独白再由文字匠拼接更有机、更贴合对话交锋（人工审查意见第 3 点）；但自由到无法收场的群聊不可控，故必须由主持人驱动 + 显式结束判据。
- **影响**：新增**围读会（round-table scene）**成为章内排演的可选子机制；`join_scene/say_line/leave_scene` 三个受控工具；群聊收敛判据见 05§5.3（活跃演员全部 leave、到达 max_rounds、主持人判定收敛等）；结束后的谈话纪要并入 `character_take` 供文字匠整合。这是对 ADR-001 的**受控例外**，不推翻"强控制"原则。

### ADR-015 LLM 输出被供应商审核拦截
- **决策**：Provider 层必须识别并处理"受内容审核拦截/拒答"的返回（云 API 常见）；策略为**分级降级**：识别拦截 → 可选改写措辞重试 → 切换 Provider/模型 → 交给人工介入 → 全部失败则挂起该片段并标记，不伪造/静默跳过。
- **理由**：引用云 LLM API 必然受厂商审核系统影响（人工审查意见第 4 点）；若不对拦截做处理，会产出"空片段/脱节章节"而非可感知的受控状态。
- **影响**：`LLMResult` 增加"审核拦截"结果类别与会话级重试策略（见 07§2.6/§8）；Provider 需上报拦截错误，编排器按预算重试/切换；敏感词预检降低命中率；审计记录拦截事件（见 09§4）。

### ADR-016 文件为主 + SQLite 辅助索引
- **决策**：持久事实源一律为**文件**（bible/outline/chapters/memory 的 JSON/Markdown，Git 可 diff、人工可编辑，ADR-004/010 不变）；在此基础上以**项目内 SQLite（`<project>/.index.db`）作为辅助索引与查询通道**承载：记忆碎片检索索引、事件流、审计日志、检查点元数据、批基线索引。
- **理由**：纯 JSON 在全书持续追加上会有"整文件重写 + 无并发读 + 无范围查询"的工程痛点（用户选型决策）；引入 SQLite（零外部服务、事务、可并发读、SQL 查询）收益明显，同时**不牺牲文件即状态**的透明度——SQLite 全部可由 JSON 事实源**重建/导出**，不算新的"事实源"。
- **影响**：数据设计（06§9）新增 `.index.db` 的 schema 与"可重建/与文件一致性"约束；存储层新增 `indexdb` 模块（见 08）；SQLite 可随时删除并从文件重建（如切到纯文件模式），ADR-004 的"文件即记忆"定位不被动摇。

### ADR-017 构建层（Forge）：Blueprint 中间态 + 双输入模式收敛
- **决策**：新增独立的**构建层（Forge）**，负责流水线前四道工序（立项/世界观/大纲/细纲）的真实产出。
  两种输入模式（一句话 `seed`、已有章节 `ingest`）**收敛到同一个中间产物 `Blueprint`（构建蓝图）**，
  再由构建引擎从蓝图长出 bible + outline；蓝图落盘为可编辑文件并带 `provenance`（字段来源：
  user / llm / template / ingested），问答与构建过程落 `transcript.jsonl`，可中断续跑。
- **理由**：立项→世界观→大纲→细纲此前是空转（`cli.py:87-99` 只推进状态字符串，bible 全库只有读没有写），
  A1 不满足、每本书的 bible 都靠手工/harness 硬编码。两种模式收敛到同一蓝图，使"模式二缺口回落模式一的
  商讨"在架构上免费；provenance 让"问什么/细化什么/披露什么"都有确定性依据而不是模型随机决定。
- **影响**：新增 `src/novelist/forge/` 模块族与 `workspace/forge/` 目录；`project.json` 增 `forge` 段；
  CLI 增 `forge seed|ingest|show|resume|build|validate`。**不引入 subagent 框架**——
  docs/05 §3 的世界观构建师/大纲师职能由本层承担，子代理框架另议（用户拍板 2026-09-01）。详见 docs/10。

### ADR-018 构建期递归深化（模型自判 + 引擎硬边界）
- **决策**：构建层采用**递归深化**而非固定步骤流水线：每个构建节点由**模型判断** `decide=done|expand`
  并给出 `reason` 与子节点清单；引擎以**四道硬边界**兜底——`max_depth`（默认 4）、`max_width`（默认 4）、
  `max_calls`（默认 80）、每节点 `max_retries=1`；每节点产出须过 schema + 交叉引用校验才落盘，
  失败回退父层产物，绝不静默跳过。
- **理由**：固定 S1→S6 分步无法适配题材复杂度差异（简单题材过度生成、复杂题材细化不足）；
  用户明确拍板"接受更多次模型调用换取更高质量"（与第七批"分层深化"同宗旨：每次调用聚焦一个小目标，
  是小模型出高质量的可靠路径）。但模型自判必须有界——第七批防失控三原则（深度/宽度/检查点兜底）
  在构建层的具体化。
- **影响**：构建期 LLM 调用数从"固定几次"变为"有上限的可变次数"（默认 ≤80），耗时上升但可接受；
  新增 `workspace/forge/nodes/` 存每个节点产物以支持续跑与调试；构建报告须披露每个节点的 `decide`/`reason`。

### ADR-019 时间线与定时事件机制（相对天数轴 + pending + 渐进提醒）
- **决策**：故事内时间采用**相对天数轴**（`worldstate.json` 顶层 `time: {now, origin_text}`），
  事件级推进由编纂员在**既有抽取调用**中输出"时间：+90日 / 闪回 / 同日"行（零新增 LLM 调用），
  中文量词归一为天数；**定时事件（pending）内嵌 worldstate.json**（`{id, who, what, due, status, created_at,
  thread?}`），登记来源为编纂员"约定："行、Forge 细纲 `key_events` 可选 `after_days`、模式二 ingest 抽取；
  **渐进提醒分档**为确定性规则（剩余 >30% 静默 / ≤30% 轻提示 / ≤10% 或到期强提示+key_events 候选 /
  到期 3 章 warn、再 2 章 block=R-TIME），只升提示强度、不硬插剧情；**闭关/失踪/昏迷登记为不可出场期**，
  期间出场 → R-STATE 告警。`bible/timeline.json` 激活为历史时点登记簿（`at: {t, vol, ch}`），
  R-TL 从按章序单调改为按 t 单调。
- **理由**：时间维度的"壳"早已存在但"泵"没接——`timeline.json` 实跑为空（proj-t5 五章后 `[]`）、
  `LandedEvent.timeline_delta`（`writeback.py:41`）零调用方、R-TL 空转；原历法式 `at:{era,year,season}`
  LLM 无法稳定维护。天数轴可归一计算、显示层再格式化（"入宗第 3 年·第 1143 日"）。
  用户提议的"时间临近节点时系统渐进注意到并引出事件"与收尾清单（payoff_checklist）同范式，
  但驱动维度不同：threads 按剧情位置（卷末），pending 按天数（世界日程），二者 id 互引不合并。
- **影响**：chronicler 抽取 prompt 加"时间：/约定："两种行；worldstate schema 扩展（time/pending/
  unavailable_until，history.at 加 t）；新增 R-TIME、R-TL 改造、R-STATE 扩展；produce_chapter 注入
  通道加"临近事项"段；Forge 细纲支持 `after_days`（回填 docs/10）。详见 docs/06 §3.3。

### ADR-020 生成期人物一致性四件套（延迟拟题 · 无条件注入 · 人物调度层 · 角色视角记忆）

> 来源：3080ti 十章实测后对 ch1/ch2 的人工长评（见 `_harness/前两章问题归因报告.md`），
> 归因结论 A 流程 65% / B 模型 20% / C prompt 10% / D 其他 5%。本 ADR 收敛其中**流程侧**的四个改动，
> 用户于 2026-09-02 逐条拍板。

- **决策一：标题延迟拟定（Deferred Titling）**
  事件级生成**一律不输出章节标题**；细纲 md 不再把 `# 第 N 章 XXXX` 写进会被模型看到的上下文；
  整章正文拼接、去重、篇幅裁定**全部完成之后**，才由一次独立的短调用（≤64 token，temperature 0.3）
  依据**成稿正文**拟题，拟得标题写回正文首行与细纲 front-matter `title`。
- **理由**：标题在生成期出现会引发两种病症——① 每个事件开头重写一次标题（实测 ch1 拼出 3 个标题，
  `_dedupe_chapter_titles` 行首 `match` 又删不掉行内/缩进变体）；② 拟题在前会反过来"锚定"内容，
  模型为扣题牺牲细纲。延迟拟题让标题成为**结果**而非**约束**。
- **影响**：`render_gist_md` 输出改为 `# 第 N 章`（不含标题）；`_event_goal`/`_beat_prompt` 增加
  "不要输出任何标题"禁则；新增 `_title_chapter()`；章末产物多 1 次 LLM 调用（全章仅一次）。

- **决策二：出场人物卡**无条件**注入（Unconditional Cast Injection）**
  本章/本事件出场人物的人物卡**不走检索、不做相似度筛选**，全部注入正文 prompt。
- **理由**：RAG 检索在人物维度上会**漏召回**，而漏召回的代价是**人设崩塌**——实测 ch2 何远戏份矛盾
  正是相关人物行未被检索命中。人物卡总量可控（每章 3–6 人 × 每卡 ~120 字），省下的 token 远不值风险。
- **影响**：`_event_goal` 的 `related.character` 保留（跨章补充），其上叠加一层确定性的 cast 注入通道；
  注入字段由原 6 项扩到 9 项（补 `arc` / `aliases` / `relationships`）。

- **决策三：人物调度层（Character Direction Sheet，细纲与正文之间的第 3 次细化）**
  在"细纲事件 → 正文"之间新增一次 LLM 调用，输入细纲事件 + 该事件出场人物的完整卡 + 该角色近况，
  输出**人物调度单**：逐角色给出「性格要点（≤3 条）/ 本事件中如何体现（语气、动作、取舍）/ 对其他在场者
  的态度 / 本事件禁忌（不得出现的行为或口吻）」。调度单作为**强制约束段**注入正文 prompt 最前部。
- **理由**：这是"人物扮演（ADR-012 角色演员）"的**离线编译版**——不引入运行时多 Agent 会话
  （不可控、成本爆炸），但把扮演要做的事（把静态人设翻译成此刻的具体表现）在生成前一次性做完。
  用户拍板"在细纲与正文之间再加一次细化，详细加上人物性格及体现"。
- **影响**：新增 `core/director.py`；每事件 +1 次 LLM 调用；调度单落盘
  `memory/directions/<vol>-<ch>-e<idx>.json` 供事后审阅（与 ADR-016 文件为主一致）。
  调度失败静默降级为仅注入人物卡，不阻断生成。

- **决策四：角色视角记忆（Perspective Memory）+ 事件末调用**
  编纂员在**每个事件落定后**（而非章末）新增一次 LLM 调用，对本事件出场人物**一次性**输出各自的
  **视角条目**：`{人物, 立场/情绪(stance), 该角色视角的事件概述, 对其他在场角色的关系增量}`。
  同一事件的不同角色条目由同一次调用产出，天然形成**视角差异**（同一件事，各人看到的不同）。
  写入 `memory/character_histories/<id>.json`（ADR-011：经历属"实然"，**不写回 `bible/characters.json`**）。
- **理由**：既有 `append_experience` 只把**同一条事件摘要**复制给所有参与者——李慕白和陈松拿到的是同一句
  "客观"描述，等于没有视角。角色视角是后续该角色出场时"他会怎么说/怎么想"的唯一依据。
  用户拍板方案 B，但改为**事件末调用**（更贴 ADR-013"事件落定即回写"，且章末一次性总结会丢事件粒度）。
- **注入与回读**：
  1. **注入**：按本章出场角色**无条件**注入该角色最近 N 条（默认 3）视角条目（含 `stance`）。
  2. **回读触发（双阈值）**：主判据用**事件计数**（该角色距上次出场 ≥3 个事件——与记忆写入节奏对齐、
     event_id 自带序号、实现零成本）；辅判据用**时间线天数**（故事内 ≥30 天未见，覆盖"闭关/远行"这类
     事件少但跨度大的情形）。二者**任一满足**即回读——回读内容为该角色最近出场**事件所在正文片段**
     （默认 1200 字 × 最多 2 段），非全文。
  3. 弃用"按章计数"：章是产物切分单位，不是记忆单位（一章 1–4 个事件不等，粒度不匹配）。
- **影响**：`chronicler` +1 次调用/事件；`character_histories` entry 增加 `stance` / `perspective` /
  `relations` 字段（向后兼容，旧条目无字段即视为无视角）；新增回读与注入装配函数。

- **成本**：以 10 章 × 3 事件计，新增约 30（调度）+ 30（视角）+ 10（拟题）= 70 次调用，
  对比既有每事件 1 生成 + 1 审校 ≈ 60 次，总量约翻倍。用户明确接受"更多次调用换取质量"。
  所有新增调用失败均**静默降级**，绝不阻断成稿。

- **后续议题（不在本 ADR 实施）**：**世界广播选角（World Broadcast Casting）**——在每个事件的选人阶段
  新增一次 LLM 调用，把事件简介、大致流程、时间地点等信息"广播"出去，由模型推理**应当有哪些人加入
  本次事件**（"谁在场才合理"/"谁此时不可出场"/"是否需要引入新人"），再与细纲声明的 cast 求并集。
  本 ADR 的 cast 仍是**细纲声明 + 事件文本命中的确定性集合**，广播选角是它的上游升级，另立 ADR
  （已立 → **ADR-021**）。

### ADR-021 世界广播选角（World Broadcast Casting，事件级出场推理）

> 来源：用户在 ADR-020 拍板时提出——"每个事件的选人阶段新增一次 LLM 调用，将事件简介、大致流程、
> 时间地点等主要信息放出，然后由 AI 思考应当选哪些人加入这次事件"。ADR-020"后续议题"段收录。
> **本 ADR 已实施（2026-09-02，M3r 之后）**：`core/broadcast.py` + orchestrator 事件循环接线
> （`broadcast_casting` 开关）。v1 默认 **False**；**2026-09-06 已转 True**——用户规则全局禁用
> pro 只用 flash，广播 `max_tokens`=16000 兜住 flash 更大思考离散，批跑
> （broadcast_fired 18/18、degrade=0、miss=0、增益 21 全 plausible）验收通过（docs/问题总账 B1）。

- **背景（为什么需要广播）**：当前事件选角是**确定性的**——`declared_cast`（细纲"出场人物"行，
  章级）+ `cast_from_text`（事件文本字面命中，≤2 人兜底）。两个缺口：
  1. 细纲 `key_events` 是一句话粒度，多数不含出场人物/场景（前两章归因报告 A 类流程主因之一）——
     字面兜底只能捞到"名字出现在文本里"的角色，捞不到**职能上该在场**的人（"宗门大比"事件
     文本里没有掌门名字，但掌门该在裁判席）；
  2. 选角是 N3 人物调度层与 N4 视角记忆的**上游**——人选错了，调度与视角都建立在错的人上。
  广播 = 事件级一次 LLM 推理"谁该在场、为什么"，向上游修正选角。
- **决策：事件循环中、注卡之前新增一次"世界广播"调用（选角推理）**：
  - **输入**：① 事件简介与大致流程（`ev_text` + 上事件接缝摘要）；② 时间地点
    （`worldstate.time.now`；地点若细纲/事件可考则给，不可考不臆造）；③ **可及角色池**——bible
    active 角色剔除 `status=dead/unknown` 与 `unavailable_until > now`（闭关/失踪/被囚/渡劫，
    与 ADR-019/R-STATE 同一数据，**确定性过滤，不靠模型记**）；④ 细纲声明 cast（硬在场基线）；
    ⑤ 上一事件出场者。
  - **输出**（结构化）：出场名单——每人附"在场理由"（**职能必需 / 关系牵引 / 伏笔相关 / 动机主动**）;
    可另给「不应出场者」名单（仅作推理披露，不参与决策）。
  - **校验（确定性兜底，防模型乱来）**：
    1. 名单 **⊇ 细纲声明 cast**——规划层意图不可被广播删除（与"key_events 不得增删"同精神）；
    2. 名单 **⊆ 可及角色池**——闭关者被点名 → 剔除并告警（R-STATE 联动）；
    3. **不得自造名字**——bible 无卡者拒绝；但广播可输出结构化**缺人需求**
       （职能/境界倾向/阵营/与在场者的关系钩子），交 **ADR-022 角色工厂**生产并注册
       （2026-09-02 用户拍板：广播先在可及池内找，找不到合适人选才向工厂要人；
       工厂未实施前仍按 v1 只告警留痕）；
    4. 事件文本字面命中的角色**必须**在名单（防广播漏读事件）；
    5. 上限：单事件 ≤6 人，超出按 细纲声明 > 职能必需 > 文本命中 裁。
  - **落盘**：广播决定与理由存 `memory/castings/<vol>-<ch>-e<idx>.json`
    （ADR-016 文件即事实源；可审"这场戏他为什么在"）。
  - **降级**：广播失败/超时/解析失败 → 静默回退现有确定性选角（细纲声明 + 文本兜底），
    绝不阻断生成（与 ADR-020 全部新增调用同一纪律）。
- **分层定位**（人物一致性栈的第 0 层）：
  ```
  现状（ADR-020）：章级细纲声明 ──┐
                   事件级字面兜底 ──┴→ match_cast 注卡 → N3 调度 → N4 视角记忆
  ADR-021 之后 ：事件级广播（语义推理）→ 名单过校验（硬约束）→ match_cast → N3 → N4
                 细纲声明 cast 保留为校验基线（不可删）；字面兜底保留为校验（必须涵盖）
  ```
  广播把"**谁该在场**"定对，N3 调度把"在场的人**怎么演**"定对——调度只在人选对时有意义。
- **候选池规模边界**：≤40 名 active 角色时全量进广播 prompt（每行"名字/宗门/一行近况"）；
  超过 40 升级 RAG 预筛 top-K（P1，本书 ~20 角色不触发）。
- **成本**：+1 次调用/事件（10 章 × 3 事件 ≈ +30 次，与 ADR-020 同量级）；输入短、输出
  仅名单 ~6 行，可给低正文预算（思考型模型仍需预留 reasoning）。
- **与相邻机制**：广播是"选角"不是"扮演"——不做多 Agent 会话，仍服从主编剧中枢；
  与 ADR-012 演员 / ADR-014 围读会无冲突（它们的入场前提是广播已把名单定对）。
- **拍板记录（2026-09-02 全部落定，★ 项按推荐默认）**：
  1. ✅ **广播名单与细纲声明 cast = 求并且细纲不可删**（★ 推荐采纳）；校验 1 强制补回漏删者；
  2. ✅ **已拍板（2026-09-02）**：新人必经 ADR-022 角色工厂（广播先找池内、找不到才提需求入队）；
  3. ✅ **广播调用独立成次**（★ 推荐采纳）——事件循环内、注卡前 +1 次调用，失败不影响调度；
  4. ✅ **广播决定落盘** `memory/castings/v{vol}-c{ch}-e{idx}.json`（★ 推荐采纳）。
- **v1 实施范围（core/broadcast.py）**：
  - 可及池 = bible 卡 − worldstate `dead` − `unavailable_until > now` − bible `status∈{dead,unknown}`
    （确定性过滤，`available_pool`，零模型调用）；
  - prompt：事件文本(≤600) + 前情接缝(≤300) + 故事内天数 + 细纲声明 + 上事件出场 + 池（≤40 行）；
  - 输出仅 JSON（`present` 每人带四类在场理由之一；`needs` 缺人需求交 ADR-022 工厂，
    `source=broadcast` 入 `character_needs_pending.json`，章前 drain 消化）；
  - 解析纪律：防造名（不在池点名 → 拒绝 + 告警）、```json 围栏容错、非 JSON 不崩；
  - 确定性校验：细纲声明 ⊇ 补回 / 池外剔除 / 文本命中补回 / ≤6 上限裁剪；
  - 失败纪律：provider 挂/blocked/解析失败 → 返回 None，调用方静默回退确定性选角；
  - orchestrator：`broadcast_casting: bool = True`（2026-09-06 由 v1 默认 False 转正），名单驱动 `match_cast`，
    计数回传 `ProductionResult.broadcasts_built`（tests/test_broadcast.py 全链路断言）。
- **v2 后续（默认转 True 已于 2026-09-06 完成）**：「不应出场者」推理披露；池 >40 时 RAG 预筛 top-K。

### ADR-022 角色工厂（Character Factory，按需生产新角色并与广播联动）

> 来源：用户 2026-09-02 提议——"引入一个角色工厂模块，可以按照需求生产特定的新角色，
> 与世界广播形成联动"。4 项拍板已全部落定（2026-09-02），**已实施**（M3q）。
> 定位：ADR-021"待拍板 2"（v1 禁引新人）的承接通道——广播仍禁造，但提名的死路被工厂接通。

- **背景（为什么需要工厂）**：
  1. v7 人物侧两病同源：无卡人物入场（云清瑶无卡无动机"突然现身"）靠模型即兴；JIT 补卡
     是**字面追认**——名字先出现在正文、事后补档，与物品侧 `supplement_settings` 反向追认
     （强化符）**同构**。P0-B 堵的是物品追认，人物侧至今只有追认、没有**正面登记通道**。
  2. ADR-021 校验 3 规定 v1 禁止广播引入 bible 无卡新人、只能提名留痕——若提名永远没人接，
     "职能上需要新角色"的正当需求（裁判席需要执法长老）会被静默丢弃。
  3. 哲学与 P0-B 一致：**新事物必须走登记，不允许追认**。工厂 = 登记通道的实体化。
- **决策：新增独立角色工厂模块**（`core/character_factory.py`），按需求生产特定新角色，
  经确定性校验后入册，生命周期挂 task #36 统一实体状态机（pending → active）。
- **三种触发源**（v1 做 ①③，② 为 v2）：
  1. **广播缺人需求**（事件级）：ADR-021 广播在可及池内找不到合适人选时输出的结构化
     用人需求 → 转交工厂生产（联动主接口，2026-09-02 拍板）；
  2. **规划层提名**（章级）：细纲明确"本章引入新人物"（key_events 或 cast 含 bible 未登记名）→
     章前预产；
  3. **人工指定**（CLI）：`novelist character new "需要一名宗门执法长老，金丹后期，与叶蓝有
     过节"`——按一句话需求生产。
- **生产管线（LLM 草卡 + 确定性闸门，闸门零调用）**：
  - **输入**：提名上下文（为何需要这个人 / 事件场景 / 在场者名单 / worldview 与 R-REALMS 要点）；
  - **LLM 产草卡**（最小卡规格，缺一不收）：name（+alias）、gender、realm（∈境界表）、
    faction（∈名册）、role、行为规格 2-3 条（可执行，非 trait 词）、**关系钩子 ≥1（必须指向
    已存在角色，默认含主角）**、本次出场动机；
  - **确定性校验**：名字不撞 bible/registry 已登记名与别名；realm/faction 在名册内；
    关系指向的角色必须存在；性别与称谓一致；近似查重（同名+同门+同境界拒收）；
  - **入册**：provenance=factory + confidence；状态 pending——**pending 角色不进广播可及池、
    不许入场**，确认后转 active 才可选角（与 ADR-019 pending 时间事件同一纪律）。
- **与世界广播的联动时序**（✅ 2026-09-02 用户拍板——**广播是需求匹配器，工厂是缺货通道**）：
  1. 广播收到当前情节的用人需求（谁在场才合理/是否需要引入新人）；
  2. **先在可及角色池内寻找**（bible active − dead − unavailable_until，确定性过滤）；
  3. 池内有合适人选 → 正常点名出卡；
  4. 池内无合适人选 → 广播**不自造名字**，输出结构化缺人需求（职能/境界倾向/阵营/
     与在场者关系钩子/为何现有的人不合适）→ **转交角色工厂**；
  5. 工厂生产草卡 → 过确定性闸门 → **注册入册（active）** → 本事件即可注卡入场。
  同步入戏的空卡顾虑由**最小卡规格**兜底：关系钩子 ≥1（指向已存在角色）、行为规格
  2-3 条、境界/宗门必须在名册内——注册的卡虽薄但合法，比无卡即兴可审计得多。
  事后人工可修改/回退（memory 修订接口，P1 范畴）。
- **与 JIT 补卡的关系**：工厂落地后 JIT **降级为告警器**——字面抓到未登记名字只报警 +
  转工厂需求队列，**不再直接补卡入库**。人物侧追认通道彻底关死（与 P0-B 合围）。
- **配额与防滥用**：每章工厂新角色 ≤2（★待拍板 2）；bible active 角色出场覆盖度不足时
  工厂停摆（P2 覆盖度告警联动）——**先演好已有的人，再生新的人**。
- **依赖与排期**：**排在 P0-A（bible 数据补喂）之后**——工厂的约束源（境界表/宗门名册/
  关系图）与草卡质量都吃 bible 数据红利；bible 饥饿时工厂只会量产空壳。
- **成本**：每新角色 +1 次调用（草卡生成）+ 纯确定性校验零调用；10 章正常 2-4 个新角色
  ≈ +4 次调用，相对 ADR-020/021 的 +30 量级可忽略。
- **待用户拍板（★ 为推荐；1/4 已于 2026-09-02 拍板）**：
  1. ✅ 联动时序 = 广播先找池内 → 无合适人选才向工厂提需求 → 工厂生产注册后本事件
     即可入场（同步，见上）；
  2. ✅ **已拍板（2026-09-02）**：每章新角色配额 = **≤2**，超出需求进队列下章消化；
  3. ✅ **已拍板（2026-09-02）**：触发源范围 = **广播缺人需求 + 人工 CLI**（细纲规划层提名 v2）；
  4. ✅ 注册方式 = 工厂过闸门后**自动注册 active**（用户口径"生产新人物并进行相关的
     注册"），事后人工可修改/回退；另保留 `status=pending` 人工预审模式作备选。

### ADR-023 人物关系账本与关系视图（A2 落地 · 设计定稿 2026-09-03，**已编码 v1 2026-09-03**）

> 背景：A2（relationships/behavior_rules 语义与"事件性"溯源）暴露三缺口——N4 视角 delta
> 是散点无状态聚合、bible 关系行是死文本不随事件刷新、无"关系状态→官方设定"升级通道。
>
> 落地实证（v1，2026-09-03）：**前提失守先修**——现网 35/35 条视角记录的 relations 全空
> （N4 第 4 段"可选"被模型长期忽略），已把 PERSPECTIVE_PROMPT 第 4 段改**必答 +
> 方向词表**（升温/降温/转向/断裂/复合，格式 `对<名>：<方向>·<变化短语>`），解析器兼容
> 旧自由文本并容忍"对"前缀、丢弃"无"；账本聚合读取 chronicler 原样存的 delta，纯确定性。

- **决策（D1/D2/D3，2026-09-03 用户逐条拍板）**：
  1. **视角事件不写进 bible 卡**（D1）——卡每事件注入，塞事件流会烧 token 且混淆
     ADR-011 应然/实然；完整 log 留 `memory/character_histories/<id>.json`（N4 已写），
     **卡只渲染"当前关系状态一句话 + 最近视角摘要 ≤1 条"**（从实然层实时取）。
  2. **策划层具备查询能力**（D1 补充，用户主张"负责策划情节的 Agent 应当能查询具体
     事件经过"）——细纲/事件规划的低频决策点可调确定性查询接口读**实然事件**
     （`memory/plot_events` + histories + timeline 序），**不靠 prev gist 计划态**（C2 修复
     的能力面）；正文高频生成仍走紧凑前情注入，不做全量查询。
  3. **新建实然关系账本**（D2，`memory/relationships.json`，按 pair 键）——事件结束后
     用 N4 已产出的 `relations[{who,delta}]` 做**纯确定性聚合**（零新增 LLM 调用）：
     `{pair, state, trend(升温/降温/转向/断裂/不变), by:{各视角 view+delta_seq},
     last_event, updated_at}`；**双方认知分开存**，客观锚点仍由 chronicler 事件承载。
  4. **阈值升级入档**（D3-b）——关系显著翻转（同向 delta 连续 ≥2 事件，或断裂/复合级）
     → 生成"关系修订提案"走 **enrich pending 通道**（人工 `--allow`，provenance 同 M3r），
     不自动覆盖 bible。
- **关系视图层（RelEdge，按需投影，不建知识图谱系统）**：bible 应然边 / chronicler 事实边 /
  认知 delta 边 三类语义不同，统一为 `{a, b, kind: 应然|事实|认知|状态, state, source_ref,
  at, trend}`，带出处/时间，按需从源文件投影（ADR-016 可再生缓存，非新事实源）。
  消费方：广播池行关系锚（B2）、注入前状态文本、一致性三元检查（E7）、enrich 前情（A3）。
  现阶段几十人规模 dict+邻接表足够；数百实体/跨卷党争再评估真图存储。
- **影响**：chronicler 事件末多一步账本聚合（确定性）；orchestrator 事件循环 hook 追加；
  注入渲染读账本状态行；`docs/08` 排期时挂 M3t 类里程碑。待编码项记入 `docs/问题总账`。
- **实现（v1，core/rel_ledger.py，2026-09-03）**：
  - 账本落 **`memory/relationship_ledger.json`**（命名偏差说明：本 ADR 原文写
    `memory/relationships.json`，但该名已被 docs/06 §3.5"关系变化事件日志"占用（含测试/RAG
    碎片语义）；账本是 ADR-016 可再生投影，另立文件、两职责不混写）；
  - `rebuild_ledger()` 章末 hook（orchestrator 5.6 节）幂等重建：pair 行
    `{pair,state,trend(升温/降温/转向/断裂/复合/不变),state_from,diverged,by{视角 delta
    seq},last_event,updated_at,flip}`，同事件多视角方向归并（断裂/复合 优先、升温+降温
    同场→转向）；`trend` 为最新事件方向；
  - D3-b 阈值：`flip` = 同向升温/降温 连续 ≥2 事件 或 单次 断裂/复合 级 →
    `enqueue_flip_proposals()` 生成"关系修订提案"入 enrich pending（人工 `--allow`，
    provenance 同 M3r）；去重（bible 行已同/pending 已有）；不改 bible；
  - C2 能力面：`memory.query_recent_actual_events()` 实然事件时间序查询，
    forge 章细纲 prompt 计划态 prev 之外追加【已落定实情】块（forge/nodes.py
    `_actual_events_block`，无正文记忆时行为不变）；
  - A3 部分：enrich 提案前情补 `ledger_lines_for()` 账本状态行（防补喂与观测趋势冲突）。
  测试 `tests/test_rel_ledger.py` 11 用例 + 受影响回归全绿；账本失败不阻断写章。

### ADR-024 人类决策介入分层（"通过问答写小说"的定位 · 设计定稿 2026-09-03，待编码）

> 背景：用户反思"主要设定只有一句话就开始写"，考虑把问答作为核心设计。判断：LLM 不缺
> 想象力（A1/广播/ch6 均自创），缺 约束力/品味/自我觉察——问答用于补约束与取舍、
> 逼出 unknowing，不是喂想象。**不采用全程问答引擎**（决策税爆炸、作者无答案时空框
> 提问劣于候选推荐）。

- **决策（三层介入谱，2026-09-03 用户认可立场，待成文拍板细节）**：
  1. **T1 必答 / 人拍板**（高杠杆·低可逆）：金手指规则条款、不可逆后果（死亡/背叛/暴露）、
     结局方向、核心关系史/禁忌——立项必答 + 触及即 gate；seed 提炼的 `unknowns`
     （如"金手指规则细节"）**不再静默放行**，转 T1 追问或授权默认。
  2. **T2 候选让选**（中杠杆）：LLM 给 2-4 候选 + 推荐值，回车即过——复用 `run_consult`
     （forge/ask.py：每轮 ≤4 问、`q` 退出续跑、非 TTY 降级推荐值）；现状只立项期生效，
     需扩展触发点与可见性。
  3. **T3 全自动**（低杠杆·可后改）：场景转场/对白/节奏/细节深化——生成+审校+润色，不回写用户。
  4. **章后批注修订环**：人工批注（现 `docs/人工审查.md` 为手动版）半结构化 →
     回写 memory/gist（依赖 P1#7 修订接口）→ 修订或下章消费——"问答"分次后移，
     非开写前一次问完。
- **影响**：A1 直接修复 = mechanic 从一句话升级为"条款子槽"（T1）；enrich/广播/正文在
  "将造新机制语义而 bible 无条款"处触发 gate 或记 pending；run_consult 触发点扩展。
  待编码项记入 `docs/问题总账`。
- **落地 v1（2026-09-03，已编码）：分模块审核闸门**。用户指令："核心设定分模块，每模块
  一个是否审核的开关，像软件的权限设置；开关开则生成后展示给用户，可行/可行且该模块
  不再审核/给修改建议重生成；新旧矛盾如实告知，正文连带修订暂缓"。
  - 模块注册表（forge/review.py `REVIEW_MODULES`，依照节点谱系分配）：book 产出按
    协议段拆 6 模块（worldview/characters/entities/threads/style/volumes），
    volume/chapter 节点各对应 outline_volume/outline_chapter。
  - 状态 `{project}/workspace/forge/review.json`：switches（默认全开，安全优先）/
    pending / history。
  - build 闸门：book/volume/chapter 节点成功后按开关落 pending + 渲染评审稿
    `reviews/<module>.md` → `_GateHalt` 走 try/finally 正常收尾（stage=review）；
    build 开头遇 pending 零调用直接交还。`gate=False` 参数供单测/脚本 bypass。
  - CLI：`forge switches [MODULE on|off]`、`forge review [MODULE]`、
    `forge approve <m> [--remember]`（--remember = 可行且后续不再审核）、
    `forge revise <m> "建议"`（book 模块定向段重生成单调用；大纲模块删产物重跑节点，
    `extra_instruction` 注入建议；新旧差异确定性 diff 如实列出；已有正文时提示
    旧文风险，自动修订暂缓——按用户指示搁置）。
  - 测试 8 例（tests/test_review_gate.py）；存量 build/run_seed 测试补 `gate=False`。

### ADR-025 线索（Line）子系统：一份账本四级视图（设计定稿 2026-09-06，**批1+批2 已编码 2026-09-06**）

> 来源：lingyu5 真机实证——宗门暗线 5 章 7654 字仅 3 次名词提及、零处暗线场景。
> threads（plot_threads）语义是伏笔（何时收），从不回答"这条线这章在不在场"；
> 章纲 threads_involved 填了但 core/ 零消费；人物有 ADR-020 调度层而线索没有。
> 全量设计见 `docs/线索子系统设计与规划-2026-09-06.md`（含外部方法论吸收与拍板记录）。

- **决策**：
  1. **账本**：`bible/lines.json` 单一文件（ADR-016），字段按 ADR-011 分应然
     （kind main/subplot/hidden、carrier、scope、target）与实然（status、opened、
     last_seen、progress 只追加、yield、closed）。主线唯一 + main.target 必填 = 硬校验。
  2. **起止三级落定**：蓝图 book 节点骨架登记（dormant）→ 卷纲 line_plan →
     章纲 `lines_present` 过人审 = 事实开启点。生成期模型只有提名权：
     Chronicler 记 pending → 下一章细纲审批人工转正。
  3. **事件始、事件终**：Chronicler 线索行（`线索：ln:x | 推 | 一句话`）+ 确定性校验；
     与 target 计划吻合 → 自动闭合留痕；计划外 → closing_candidate 章末人工确认；
     closed 留档不删，死线复活确定性拦截。
  4. **注入 = 一份账本四级视图**：卷纲（dormant 全集 + 前卷 yield 回流）/ 章纲
     （active 全量单行卡 + 分档冷却告警 + closed 禁复活负清单，唯一决策层）/
     事件生成（只给本场命中线卡 ≤3 条 + 交织标注，**钉死层 priority -1**，
     不再依赖 RAG 命中）/ 润色（<50 字禁令）。不做 RAG 依赖注入。
  5. **一硬多警**：主线唯一=硬；活跃支线≤3 / 收尾期禁开线 / 开篇期 hidden 禁揭开 /
     冷却超阈（main 3、subplot 5、hidden 跨卷合法）= 告警。微线不入账。
  6. **卷弧五元组**：卷纲 summary 升 goal/obstacle/outcome（允许受挫）/cost/bridge
     ——治"每卷必全胜"（老问题 C1）。
  7. **伏笔↔线索生命周期衔接**（批2·外部方法论吸收）：threads 增可选 `carrier`
     （object/goal/character/...）；卷末审计对"已回收 + 登记了 carrier"的伏笔
     register_pending 提名升级为线（"令牌回收后持续出场 → 物线索"），人审转正。
     threads 增 `due`（target_vol 到期未回收，卷末审计写回 → 下卷卷纲强制项）。
  8. **回声与检查点**（批2）：dormant 线静默超 max(8, K) 章 → 回声告警（建议
     flicker 轻提及，防"令牌整卷躺背包读者忘光"）；卷中 ~50% 章检查点对 active/
     suspended 线本卷零推进者写 `due` 强制处理项（chapter_view 置顶 + 卡片标注，
     章纲动作/正文回写触及即清算）；卷末审计（伏笔到期/回收升级提名/yield 缺失/
     主线篇幅比 <30%）落 `reports/lines-audit-vol{N}.md`，全部确定性零 LLM。
  9. **细纲修订 replay**（批2·revise 通道转正）：人工改细纲「本章线索: [...]」后
     `forge lines-replay V C` 重放——旧声明在本章留下的状态确定性回滚（open→
     dormant、进度行删除、本章闭合撤销），新声明重新落账；账本与细纲始终一致。
- **实施**：批1（阶段1-4 数据层/规划侧/生成注入/回写闭环）+ 批2（阶段5 检查点/
  审计/回声/replay + 借鉴三项 carrier/due/埋设提示）均已编码 2026-09-06；
  测试 +28（tests/test_lines.py），全量 774 passed。阶段6 真机验收待做
  （云服务器关闭期间未做任何依赖远程模型的验证，单测全走 fake provider）。

### ADR-026 承诺账本 + 未来窗口滚动细纲（恒定/可变层 · 设计定稿 2026-09-06，**已编码**）

> 背景：细纲到正文一路"可改"。若不设边界，作者/引擎在章节级修订时会**静默改写已对读者/
> 主线做出的承诺**——伏笔还没兑现就被删、卷主线变了、核心人设漂移。需要一个确定性闸门：
> 哪些层**恒定不可改**（承诺账本），哪些层在什么窗口内**可变可自动落盘**。
>
> 一句话：「已发生的承诺不可静默改；未发生但已对读者显影的伏笔，若要改动必须过人工审核；
> 其余未来 2-3 章窗口内的执行层反复横跳，全部自由。

- **承诺账本（Covenant）——只读"恒定 vs 可变"边界视图**（`forge/covenant.py`）：
  由**蓝图 canonical 集**确定性汇聚，非新事实源：
  - **恒定层**（承诺触即 gate，绝不静默改）：已埋且在途的伏笔（`threads.status ∈
    {planted, pending_return}`，guard `status,target_vol`）、卷主线（`volumes` guard
    `summary,key_beats`）、核心角色人设/弧线/结局（`characters` guard `role,arch,
    ending`）——即"恒定 vs 可变"的恒定侧。
  - **可变层**：未来刻度上尚未成文的 key_events 排序与实现手段、单章节奏。
  - 判定 `touched_entries(bp_old, bp_new, covenant)` **全确定性**（逐 guard 字段取值比较
    + 整条被删），零 LLM、零配额；`affected_modules` 映射到审核模块 id。
- **未来窗口滚动细纲**（`engine.roll_window`，CLI `forge roll-window <vol>`）：
  - 故事写到卷内某处后，修订**尚未成文的下 N 章**（缺省 3，`--width`）的 key_events 等
    执行层，使细纲贴合已发生事实——卷间/章节间**纠偏入口**。
  - **门控**：改动先过 `touched_entries`。未触及承诺 → 自动落盘（细纲 md + 蓝图 chapters）；
    触及 → **回滚**窗口改动 + `mark_pending` 人工闸门（复用 ADR-024 分模块审核）——
    绝不静默改承诺。
  - **边界**：窗口越过卷尾钳制在卷内（`--from` 缺省 = 卷内第一未写章自动定位）；已写章跳过；
    整卷写完提示用 `forge roll <vol+1>` 衔接下一卷；每次滚动前自动打快照（F5 双快照基线）。
- **影响**：写正文到中途也可安全纠偏细纲，不用整卷重来；风险边界清晰。
- **实现（2026-09-06）**：`forge/covenant.py` + `engine.roll_window` +
  CLI `forge roll-window` + `forge covenant`；测试 `tests/test_covenant.py`
  （13 例）+ `tests/test_m21_roll_window.py`（7 例）全绿。

### ADR-027 记忆 LLM 侧选 rerank（检索质量增强 · 设计定稿 2026-09-06，**核心已编码**）

> 背景：`MemoryRetriever` 打分 = 相似度（`VectorEmbedding` 余弦 / `KeywordEmbedding`
> 精确 token）× 事件类型权重。它擅长"字面/分词重合"，但**"分词重合度低、语义却相关"**
> 的碎片会被漏在前门——尤其在关键词模式（无真 embedding 时），Novelist 已在
> `_keyword_score` 注释里记下哈希向量噪声的教训，检索质量是登记在案的痛点。
> 借鉴 Claude Code 的 s09（记忆选择用 LLM side-query，而非 embedding）：在相似度之上
> 再叠一层 LLM 相关性精审，补回纯相似度追不回的语义相关。

- **形态**：`MemoryQuery` 增加可选 `reranker` 回调 + `rerank_pool`；回调在
  `MemoryRetriever.query()` 打分排序后、返回前执行。**回调=None = 完全保持现状**
  （确定性高达逐字节、零 LLM、零配额）。
- **候选池放大**：rerank 开启时先从全部已过滤候选取 `top_k×3`（至少 6）作候选池——
  否则侧选只能"重排已被相似度认为相关的"，救不回漏检。池外碎片按原分回补。
- **产物**：回调返回 `(sig, reason)` 降序；`reason`（该条为何被优先）写入
  `MemoryHit.reason`（默认空=未 rerank），供一致性审查/审计。
- **注入**：回调经注入，非内建 LLM（与 `MemoryWriter.semantic_checker` 同模式）；
  编排层负责把它接到"判断类·thinking 路由"（dp-thinking-policy）。异常→静默回退纯相似度。
- **影响**：写正文/一致性审查的"该忆没忆、不该忆乱忆"降低；风险=多一次 LLM 调用
  （判断类，接受）。逐开关默关，真机对照后再转正。
- **实现（2026-09-06）**：`core/memory.py` `MemoryQuery.reranker/rerank_pool` +
  `MemoryRetriever._rerank` + `MemoryHit.reason`；测试 `tests/test_m22_mem_rerank.py`
  5 例 + 既有 `test_m3_memory.py` 22 例全绿。编排层 LLM-backed 注入待真机验证阶段接线。

### ADR-028 任务级持久化 + 编排器指派 owner + can_start（崩溃单任务恢复 · 设计定稿 2026-09-06，**核心已编码**）

> 背景：`pipeline_state` 是阶段级**单个字符串**，没有卷/章/事件粒度——崩溃/中断只能整卷
> 快照恢复，无法"续那章半成品"。借鉴 Claude Code s12 的 Task System（细粒度任务 JSON +
> blockedBy 依赖 + owner + 文件锁），但**适配 Novelist 的中央编排哲学**：不做"多 Agent
> 自看板认领"，而由编排器显式指派 owner + 显式 can_start 前置就绪检查。

- **形态**：`core/tasks.py` 的 `TaskStore`——每个卷/章任务落一个
  `{project_id}/tasks/{safe_id}.json`（Windows 安全文件名）；`Task` 记录
  `id/kind/ref/status/owner/dependencies/output/meta/时间戳`。
- **状态机**：`pending → in_progress(owner) → done`；`blocked`（承诺门/依赖挂起）、`failed`。
- **owner**：`start(task_id, owner, *, precheck=...)` 由**编排器**指派；防重入——任务在途且
  被他人持锁 → `TaskBusyError`；同 owner 幂等。
- **can_start**：显式依赖就绪检查（依赖全 done），返回 `(ok, blockers)`；下游解锁依赖上游 done。
- **covenant 衔接**：`start` 接受注入的 `precheck` 回调（如 `touched_entries`），不通过 →
  置 `blocked` 抛 `TaskError`；本模块不内置 LLM/不依赖 covenant（确定性）。
- **崩溃恢复**：`recover(policy="list"|"redo")` 扫出"in_progress 未 done"半成品及下游，决定续写或重做。
- **进入方式**：核心任务板与我不在重构段落的 `orchestrator.py` 耦合——**核心层已就绪，
  orchestrator 推进卷/章的接线按 docs/11 §13 结合真机验证阶段一起做**，避免在整改区叠债务。
- **实现（2026-09-06）**：`core/tasks.py`；测试 `tests/test_m23_tasks.py` 8 例全绿。

### ADR-029 设定集人工反馈通道（人工意见 → 字段级定位 → 审批 → 原子写回 · 设计定稿 2026-09-07，**批次A M3z**）

> 背景：创作方向确定为"系统撰写初稿、人工修改得正文"。但 bible 迄今只允许受控补喂（character_enrich），
> 人工想**按自己口径修正设定**时，没有"把自由语意见精确落到对应 JSON 字段"的一等通道；直接手改 JSON
> 会绕过 schema 校验、provenance 与 covenant 门禁。本 ADR 借鉴 character_enrich"提案→pending→入档"
> 范式，但**复用 ApprovalQueue 作为唯一人工确认队列**（2026-09-07 拍板）。

- **形态**：`core/bible_feedback.py`。一条修改请求 = `EditOp`：`{id, file, path, op(add|edit|delete|rename),
  value, reason, sensitive, status(draft|pending|applied|rejected), created_at}`。
- **目标文件白名单**：`file` 必须是 `core/context.load_bible()` 枚举出的 bible 文件（世界观/人物/文风/地点/
  线索/道具/技能/设定/世界状态等）；`path` 必须命中 **bible 可改性字段清单**（`bible_editable`：声明每类
  文件允许人工改的字段 + 只读保护区）。**只读保护区仅锁结构性身份/派生字段与实然状态**（id、is_protagonist、
  登场记录、主角约束、time 轴）——这些真不能改；**世界铁律/已提交线索属 covenant 而非只读**，是"可改但标
  sensitive 强制人工确认"（见敏感检测），保证"最终设定集与用户一致"不被只读锁死。
- **解析**：`FeedbackParser` 收人工自由语 → LLM（判断任务、开 thinking，遵循 providers 单轨纪律）拆成 op 集 →
  确定性校验（文件名白名单 + 路径可改性 + 值类型）→ 生成 `EditOp`；解析输出无 JSON/坏 JSON 视为抖动自动重试，
  结构性错误（空意见/审核拦截）直接报错——解析不出就明确提示用户补述，不静默跳过。
- **审批队列（复用 ApprovalQueue）**：每个 op 以 `tool="bible_feedback"、params={op_id,...}` 提交到
  `ApprovalQueue`（persist_dir=项目 `workspace/feedback`，pending_approvals.json 持久化）；**批次A 一律待人工确认**
  （最小风险），后续再逐步开放平凡改动的 auto-apply 白名单。
- **敏感检测**：op 命中 covenant 承诺账本（世界铁律 worldview.rules / 已提交情节线 thread.status、committed /
  time 轴 / 任意 delete）→ 标 `sensitive`，人工确认时高亮提示——落实"触碰 covenant 承诺账本必须人工 approval"
  的硬约束（ADR-026 衔接）。
- **写回**：`feedback --apply <id>` → 先 `ApprovalQueue.decide(allow=True)` 成功 → 再执行 `Workspace.write_json`
  原子写（临时文件 + rename，docs/06 §7）+ 写入幂等（已 applied 不重写）；失败回滚并留 error 标记；
  `--deny <id>` 标记 rejected、不写。provenance 落在 ops.json 审计（每条写回记录 origin=feedback）。
- **CLI**：`feedback <dir>`（交互收意见或 `--opinion`）/ `--apply <id>` / `--deny <id>` / `--list`；
  解析走 `--provider`（复用 providers 单轨 + api-key/base/model 透传），写回不调 LLM。
- **实现（批次A，M3z，2026-09-07）**：`core/bible_feedback.py` + `cli feedback` + bible 可改性字段清单
  + `tests/test_m3z_feedback.py`（17 例全绿）。

### ADR-030 草稿溯源（Draft Provenance · 设计定稿 2026-09-07，**批次B 已完成编码**）

> 背景：批次 C 需要"识别用户改了什么"，前提是知道"这份草稿当时基于什么生成的"。现 `build_system_prompt`/
> `build_chapter_context`（core/context.py）已返回 `ChapterContext.meta={vol, ch, cast_ids}`，只要把它扩展成
> **生成时快照清单**落盘即可，不新增抽象。

- **形态**：每章草稿一份源清单 `drafts/chapters/<vol>-<ch>.src.json`（与 `<vol>-<ch>.md` 同目录平行）。
  实际结构（`core/draft_provenance.py`，只读事实源 + 确定性重算 cast，**零额外 LLM**）：
  `{version, chapter:"<vol>-<ch>", vol, ch, generated_at, content_chars, mode, provider, model,
  prompt_fingerprint(sha256=systep+goal+正文), characters[{id,name}], plot_threads[{id,name,status}],
  world_rules[{id,text}], power_system_levels, settings[], memory{recent_events, fragment_count,
  this_chapter_refs}}`。
- **时机**：`produce_chapter` 成功出口（正文落盘后）原子写一份快照；单独 try——溯源失败绝不影响草稿已落盘。
- **用途**：① 向用户汇报"本章基于哪些内容生成"（CLI `draft [DIR] [vol:ch] [--text]`）；② 批次 C 的
  diff 基线与归因数据源；③ 续写时保证"下一章承认上一章已发生的事实"。
- **实现（批次B）**：`core/draft_provenance.py`（build/write/read/render）+ `produce_chapter` 接线 +
  CLI `draft`；复用 forge Blueprint 的 provenance 思路（ADR-017/018），但不侵入正文文件本身。
- **测试（批次B）**：`tests/test_m3zb_draft_provenance.py`——聚合/指纹变化/空工作区健壮/往返路径/渲染/接线
  成稿自动落源清单，共 8 例全绿。

### ADR-032 判断型子代理 Agent 化：统一证据基座 + chronicler/reviewer（· 拍板定稿 2026-09-07，**已实现 F0–F2**）

> 背景：系统只有主编剧（`chapter --loop`）走 `AgentRunner`，其余"子角色"（记忆编纂员 chronicler、审校师
> reviewer 等）都是**函数内嵌单轮 LLM**，任务一旦需要"多轮查证→决策→修正"（记忆冲突仲裁、审校求证）就
> 力不从心。方向（2026-09-07 用户拍板四选）：**第一批只升级 chronicler + reviewer；建统一子代理基座；
> 章内同步运行；Agent 只建议不直写（落库仍走确定性闸门，敏感项提请人工）**。forge 构建因本身是确定性递归
> 调度（形态不同：Agent 指挥 + 引擎执行）**延后单独评估**，不并入本批。
>
> **前提修订（2026-09-15 审计 AG-2/AG-3）**：本 ADR 的证据环**依赖原生 function calling 真正接线**。
> 审计发现此前工具定义从未下发（`AgentRunner._decide` 不传 `tools=`、`list_defs()` 全库零调用）、
> 且消息协议缺 `tool_calls`/`tool_call_id`/`role="tool"` —— 于是真机上"证据环"退化为单轮直出
> （测试之所以全绿，是 fake/scripted provider 直接吐 `tool_calls`）。现已修复：工具定义按 OpenAI
> function 格式随请求下发（仅当 `capabilities.tool_calling` 为真），assistant 回传 `tool_calls`、
> 工具结果以 `role="tool"` + `tool_call_id` 回灌；子代理证据环"回退/轮次耗尽"必须显式留痕
> （`ArbitrationResult.degraded/note`、`AgenticReview.note`），不再与"无冲突"混为一谈。

- **形态**：扩展 `core/agent_runner.AgentRunner` 成"证据循环"子代理基座——每轮 LLM 可先经 `ToolRegistry`
  观察面（读 圣经/记忆/草稿：`filesys.read` + `memory_tools.query_memory`）作为证据，再做决策；判断类
  任务**开 thinking**（`_decide` 现以 `self.thinking` 透传，可按角色配，TestScript injectable）；输出结构化
  结果（事件候选 / ReviewIssue / 建议），**不直接落库**。
- **复用**：预算（Budget）、权限面（按角色裁剪 registry）、轮次上限沿用 editor agent 语义；决策者可注入
  （`fake/scripted`，PatchDecision），单测不真调 LLM（docs/09 §2.1）。只读证据 registry 由
  `tools.EVIDENCE_READ_TOOLS` / `evidence_tools` / `evidence_registry` 提供（排除 write_*/reindex_memory）。
- **Chronicler 升级（F1，已实现）**：`core/chronicler_agent.py` —— 保留 tuned `extract()` 抽候选，
  新增 `arbitrate()` 只读证据环（query_memory/get_character_history/read_file 取证后裁决 保持/忽略/改写/提请），
  `agentic_chronicle()` / `Chronicler.run_agentic` 统合“抽取→仲裁→确定性闸门落库”；编排事件级/章级接入，
  `produce_chapter(agentic_chronicle=True)` / `chapter --agentic-chronicle`。未提及即保持；仲裁失败回退原全集。
- **Reviewer 升级（F2，已实现）**：`consistency/reviewer_agent.py` —— 复用 `REVIEW_PROMPT` 输入口径，
  以证据环在上报前 `query_memory`/`read_file` 核实可疑点，最终仍输出同格式 `ReviewIssue` 行（`Reviewer._parse`
  解析，下游零改动）；证据环失败回退 `Reviewer.review`。编排事件级接入，
  `produce_chapter(agentic_review=True)` / `chapter --agentic-review`。
- **写权限（拍板·推荐）**：Agent 产出候选/告警+证据链；**最终落库与门禁仍由确定性层负责**；命中敏感项
  （covenant / 记忆冲突 / 删除）复用 ApprovalQueue 提请人工。改的是"怎么取证"，不改"谁有权落库"。
  证据环 registry 结构上**不含任何写库/写文件工具**。
- **编排判据与保真护栏（2026-09-07 拍板）**：
  1. **切换/调度风格（丙+乙混合）**：工序出口尽量做成**确定性出口谓词**（可审、可控）——条件满足才进下一
     工序，不满足则留在本节点再跑内部取证或提请人工；复杂度不可量化的判断（质量够不够、是否重写）才交给
     主编剧 LLM 判定；**子代理只做证据增强、不决定工步**。不把"何时换 Agent"交给子代理自裁。
  2. **效果保真（A/B 双轨抽查）**：对冲突/可疑 `vol:ch` 显式跑"旧直出 vs 新 Agent 化"两轨，比对
     `report.written`/issues 与证据 `evidence` 差分，量化新路径是否劣于旧路径。抽查级、默认关（跑两遍
     较慢），作为人工复查与回归的复核工具。
  3. **取证预算（收紧+可配）**：chronicler/reviewer 子代理取证轮次上限默认收紧（6/8），宁漏勿误杀、控
     耗时与 token；经 `produce_chapter(agentic_*_rounds)` / `chapter --agentic-{chronicle,review}-rounds`
     显式覆盖。
- **里程碑**：见 docs/08 `M3aa`（子代理 Agent 化，F0 基座 / F1 chronicler / F2 reviewer 已完成，F3 文档+提交）。
- **验收**：F0–F2 单测（`tests/test_m3aa_*`）共 25 例，全量非 slow 回归 926 passed；既有 tool-loop 语义不变。
- **风险**：章内同步使单章/审查耗时上升（多轮 LLM）；用预算与轮次上限收敛（max_rounds 默认 6/8）；开关默认关，
  需显式 `--agentic-chronicle/--agentic-review` 启用，量产后评估后再转默认。

### ADR-031 人工修订识别 + 归因回写（Edit Attribution & Writeback · 设计定稿 2026-09-07，**批次C 待编码**）

> 背景：人工把草稿改成了正文，系统需要知道"这些改动哪些影响设定/事实/记忆"，再决定是否回写。不识别就丢
> 失了人工改动的唯一权威信号（人改过的地方就是该落定的 canon）。

- **形态**：`draft revise <vol:ch>`——以该章草稿源清单（ADR-030）为基线，对人工改后正文做**段落级 diff**
  （difflib.SequenceMatcher），把改动聚成 `ChangeSet`：每段 `{kind: added|removed|modified, text,
  attribution}`。
- **归因**：LLM（判断任务、开 thinking）把每段改动分类并映射到目标文件——
  a) 纯文风润色（不触发回写）；b) 剧情/事实变更（回写 memory，ADR-013 路径）；
  c) 新增/改写设定（**复用 ADR-029 的 EditOp 管线**进审批：新人物→characters.json add，改口风→style 等）；
  d) 线索状态变化（更新 plot_threads，敏感）。
- **门禁**：命中 covenant（world rules / 已提交 thread / 记忆冲突）→ 复用 ApprovalQueue 强制人工确认；
  平凡防抖类（纯排版）直接忽略。每条回写同样走 ADR-029 的原子写 + 审计。
- **实现（批次C）**：`core/draft_revise.py`（diff + 归因 + 复用 bible_feedback.apply）+ `cli draft revise`；
  diff 与归因结果需可审（ChangeSet 落 `workspace/revise/<vol>-<ch>.json`）。耦合点：改草稿不受门禁，
  **回写到圣经/记忆才受门禁**，与 ADR-029 一致。

###（草稿占位）用户画像 memory（User-Profile Memory · Backlog，待立项）

> 方向（延续批次 B/C 的输出）：跨项目的用户级记忆，沉淀文风、修改偏好、工具使用习惯；批次 C 的修订历史中
> 反复人工改的行为可蒸馏成该用户的 craft 偏好/禁用词，特定场景可固化为可注入的 skill（对应 `craft/cards/*.md`
> 注入机制）。落点：**全局 gitignored 用户级目录**（`~/.novelist/user_profile/`，跨项目），与项目工作区里的
> 设定集分开；实现前单独立项评估 schema 与隐私边界。

### ADR-033 Forge 硬边界规模适配：宽度分级 + 调用预算规模推导（· 拍板定稿 2026-09-10，**已实现 A/B**）

**背景/问题**：从总大纲拆长篇小说时，既有硬边界对**广度与调用量**不足——`max_width=4` 单一值
对所有节点类型生效，`character_group→character`/`worldview→system` 这类**列表型长尾**会被硬截到 4
（长篇角色二三十个、体系五六个，直接漏人/漏体系）；`max_calls=60/40` 是固定"单次运行配额"，不随
卷数 N、每卷章数 K、角色数 M 自适应，大 N/K 靠多次 `resume` 续跑（慢闭环）；且**无"规模达成出口
谓词"**，宽度/预算耗尽时静默缺料只靠 warnings。

**决策**：
- **A · 宽度分级**：`character/system/setting_entry`（长尾列表型）放宽到 `max_width_list`（默认
  **12**），条目型（`volume→arc`、`chapter→beat`）维持 `max_width=4`。按父 kind 经 `CHILD_KIND`
  判定（`_child_width`）。经 `forge seed/build --max-width-list N` 覆盖。
- **B · 调用预算规模推导**：`max_calls` 不显式给时按 `max(12 + N*3 + K*2 + min(M,24), 12)` 推导
  （`_build_call_budget`），卷数/章数取自 `bp.meta.scale`、角色数取 `bp.characters`；`--max-calls`
  显式给则直接采用、不再推导。N=5/K=30/M=30 → 111，超出旧固定 60。
- **C · 出口完整性检查**（**待定·未实现**）：非 LLM 校验"每卷章数、角色数 ≥ blueprint 承诺"，缺口
  显式暴露而非静默缺料。列为后续项。
- **D · 人工大纲审核门**（**建议方向·待拍板**）：构建后把大纲骨架交用户审阅，**由用户决定是否细化/
  深化**到哪一层（批注深化指令/停止），而非全由深度参数代拍。属"人工决策点"增强，贴合本系统"以
  人工决策为核心"的基调（与 ADR-029 反馈、Forge 商讨 ask 一脉相承）。形态与触发点待立项拍板。

**验收**：`tests/test_m3aa_forge_scale.py` 7 例 + `test_m17_forge_f4.py` 截断用例改写；全量 933 passed。

### ADR-034 跨工具 AI 规范与技能分发：单一源 + 薄桥接 + 机械门禁（· 拍板定稿 2026-09-12，**已实现**）

> 背景/问题：本项目大量依赖 AI 编码 agent 推进，但"约定"此前散落在 `AGENTS.md`、`docs/13`、
> 人机对话、以及各自的本机记忆里。实测后果有三：① 同一事实多处不一致（ADR 编号止于 031/019、
> 测试数 5 处不同、`broadcast_casting` 默认值自相矛盾）；② 文档基线失效无人察觉
> （`docs/11` 写 src 8047 行，实际 29031，差 3.6 倍）；③ 一个未标 `slow` 的用例长期在默认套件里
> 真调外部 API。**根因不是"没写规范"，而是规范只是文本、靠自觉执行**。
> 同时团队其他成员用不同 agent（Claude Code / Cursor / Copilot / Codex），而本机目录
> （`.workbuddy/`、`.claude/`）不入 git → **规范与技能传不出去**。

**决策**：

- **A · 单一源**：规范唯一源 = 仓库根 `AGENTS.md`（跨工具事实标准，Linux Foundation 治理，
  Codex/Copilot/Cursor/Cline/Zed/Amp/Jules 原生读取）；技能唯一源 = `.ai/skills/<name>/SKILL.md`
  （Agent Skills 开放标准，与 Claude Code 同构）。**两者都入库**。
- **B · 薄桥接**：`CLAUDE.md` 写 `@AGENTS.md`、`.github/copilot-instructions.md` 写指针，各 1–5 行，
  **禁止复制规范正文**——同一事实写两处必然漂移。
- **C · 原生装载**：`.workbuddy/skills` 与 `.claude/skills` 用**目录联接（junction）**指向 `.ai/skills`，
  由 `scripts/ai_bootstrap.py` 幂等建立：**一份源、两处原生可见、零复制零漂移、无需管理员权限**。
  （不支持联接的环境可 `--copy` 降级，但会引入漂移风险，属例外而非常态。）
- **D · 三层门禁**（本 ADR 的核心主张：**每条约定配一个机械检查；文本规范会腐烂，门禁不会**）：
  - L1 CI `.github/workflows/ci.yml` —— **Python 3.11**（`requires-python` 下限，也是能抓到
    PEP 701 类问题的版本；`forge/nodes.py` 真出过这个 bug）；
  - L2 `scripts/check.py --quick`（<3s，经 `core.hooksPath=.githooks` 挂 pre-commit）；
  - L3 `scripts/check.py --all`（+ pytest 全量与文档基线；提交前与 CI **共用同一条命令**）。
  - 沙箱环境变量（`CODEBUDDY_SAFE_DELETE_*`）由 `check.py` 注入 —— 让"该设的环境变量"只有一个来源。
- **E · 边界（刻意不做）**：门禁项**宁可少而稳**。不做零引用/死代码扫描（零引用 ≠ 该删，
  `core/scene_tools.py` 是设计预留件）与全库"测试数"正则扫描（里程碑历史数字本不该报警）。
- **F · 治理**：规范与技能等价于"生产配置"——改动影响每个人的 agent 行为，故走 PR，
  见 `.github/CODEOWNERS`。

**实现**：`AGENTS.md`（唯一规范源，§0–§9）| `CLAUDE.md` / `.github/copilot-instructions.md`（桥接）|
`.ai/skills/`（技能源 + README）| `scripts/ai_bootstrap.py` | `scripts/check.py`（G1–G5）|
`.githooks/pre-commit` | `.github/workflows/ci.yml` | `pyproject.toml` 的 `[tool.ruff]`（锁定现状）；
`docs/11` §13 P2-9 改为"ruff 已落 / mypy 与扩规则留独立批次"。

**已知边界**：`git config core.hooksPath` 是**本机配置、不随 git 分发**，故每个 clone 需跑一次
`ai_bootstrap.py`（已写进 `AGENTS.md` §0，agent 读到会代为执行）。CI **刻意用 windows-latest**：
本项目尚未验证 Linux 可移植性，门禁的职责是"因真实缺陷变红"而非"因平台差异变红"，
待可移植性验证后再加 Linux job。

**验收**（2026-09-12）：`python scripts/check.py --all` → **G1–G5 五项全绿**；
全量回归 **967 passed · 1 skipped · 1 deselected**（80.22s）；G5 的 9 项基线指标与 `docs/11`
逐格一致；`ai_bootstrap.py` 建链后两个工具的目录各可见 2 个技能。

### ADR-035 原始调用日志（CallLog）：完整 prompt/响应逐条留痕，产物问题可溯源（· 拍板定稿 2026-09-15，**已实现**）

> 背景/问题：系统的既有记录都是**结构化结果**——正文审计（`reports/stats/generation-*.md` +
> `.index.db audit_log`）只记调用次数/token/成本；forge `transcript.jsonl` 记问答事件且 value 截断。
> NFR-5 承诺"每次 LLM 调用均有审计日志"，却**没有任何地方留 prompt 原文与模型原始返回**；
> 一旦生成产物有问题（设定矛盾、漏伏笔、格式化损坏），无法回答"这次到底发了什么、模型原样
> 返回了什么"。实测溯源只能靠猜或重跑（重跑还可能不复现）。**根因：记录层抓的是解析后的
> 产物，不是调用本身。**

**决策**：

- **A · 统一拦截点**：在 `OpenAICompatibleProvider.complete()`（`providers/openai.py`）包一层
  日志——openai/deepseek 及全部 `PRESETS` 厂商（qwen/kimi/glm/anthropic/ollama/vllm/custom）都经
  此基类，一条路径全覆盖；**fake 等测试替身不走此点，不会在测试里刷盘**。
- **B · 独立目录 `raw-calls/`（相对进程 CWD；`NOVELIST_CALLLOG_DIR` 环境变量或
  `enable_calllog(dir)` 覆盖）**，按 `YYYY-MM-DD.jsonl` 分文件、逐行一个调用，**跨项目集中可查**；
  **不入 git**（`.gitignore` 已加）。刻意不放进工作区 `<proj>/logs/`——那里按项目隔离，而溯源要
  跨项目 grep。
- **C · 记录内容（宁全勿缺）**：完整 `messages`（含思考模型 `reasoning_content`）+ 实际请求体
  `payload`（temperature/tools/thinking/max_tokens/response_format）+ 原始响应 `raw_text`
  （脱敏全文）+ 解析 `result`（content/reasoning/tool_calls/usage）+ status_code + 耗时 + 异常
  （type/message）+ 时间戳。
- **D · 脱敏双保险**：payload/原始文本/异常串经 `redact_message`（providers/secrets）+ 正则
  `redact_secrets`（core/calllog）兜底，密钥/Bearer 永不落地。
- **E · 上下文归属（溯源定位键）**：`core/calllog.call_context(label)` 上下文管理器压"正在做
  什么"链（如 `chapter v1-ch3 / forge:build / forge:character`）；provider 记录时带上栈顶串。
  锚点挂在高价值入口：`produce_chapter`（卷章层）、forge `run_node`（节点层）、
  `build/roll_window/seed/consult/ingest`（构建层）。
- **F · 容错**：未 `enable_calllog` 时 `record()` 为 no-op；**记录失败绝不阻断生成/回写**
  （与 `_write_generation_audit` 同哲学）；线程安全（`threading.Lock` 串行 append）。

**实现**：`core/calllog.py`（写入器+上下文栈+脱敏+no-op）| `providers/openai.py#complete`（拦截点）|
`orchestrator.produce_chapter`、`forge/*.py` 各 `*_impl` 薄包装（锚点）| `.gitignore` `raw-calls/` |
`tests/test_calllog.py`。

**验收**（2026-09-15）：定向回归（calllog/m0/forge console+shell/roll/covenant/factory）**95 passed**；
全量 **1028 passed**；`test_calllog.py` 断言完整 prompt 全文、原始 raw_text、解析 result、异常路径、
451 拦截、`ctx` 归属均入 `raw-calls/<date>.jsonl`。未启用时不产生任何 `raw-calls/`。

### ADR-036 常驻对话 Agent（自然语通道）：console 嫁接 AgentRunner，三级门禁即自主边界（· 拍板定稿 2026-09-19，**已实现**（M3ac-1…4 落地；M3ac-5 真机验收并入批次 D））

> 背景/问题：编码 agent（Claude Code / CodeBuddy）的常驻对话范式——用户说意图，agent 自主
> 规划、调工具、观察、再调、交付——在 novelist 里完全缺位。现状三个构件各缺一截：
> `novelist console` 是常驻 REPL 但纯键盘命令分发器（非 `/` 输入直接拒绝，LLM 零参与，
> 当时是刻意设计，`shell.py` 原话"不授予 LLM 任何自主控制权"）；`forge shell` 的自由语只经
> **单次** LLM 分派写槽，没有多轮工具循环；而完整的 Agent 循环 `AgentRunner`（FC 真接线、
> 观测有界、成本记账、converge 兜底）只在 `chapter --loop` 被调用且从未真机跑过。
> **缺的不是 Agent 循环，是"对话入口 + 工具面补齐"。**

**决策**（2026-09-19 四项拍板 + 既有纪律沿用）：

- **A · 入口：console 内加自然语通道，不新建 REPL**（方案对比见下）。`/` 开头的输入维持现有
  键盘分发一字不动（确定性、可预测）；自然语输入喂给 `AgentRunner` 对话循环。console 已有
  项目导航（`ConsoleState.project_id`）+ `FilterableIO`（可注入可测），是现成宿主。
  备选 B（独立 `novelist agent` 命令）重复 console 约 80% 的 REPL 基建；备选 C（升级
  `forge shell`）定位错位——shell 是设定槽位填充器，ADR-032 的确定性护栏不能毁掉。
- **B · 自主边界 = 三级门禁现成复用**：safe（读）自主；sensitive（写草稿等）进
  `ApprovalQueue` 在 REPL 当场问用户；danger（publish/delete）默认拒、按策略文件放行。
  创作类命令（chapter 级）默认敏感级起步——**自主调 `chapter` 等于自主花钱**。
  不引入 plan mode（每轮计划确认），那是第二层体验优化，不是第一版的边界机制。
- **C · 第一版工具面 = 现有 10 个 + 结构化查询**：流水线命令（chapter/build/roll/review）
  **暂不**包成 Tool——先跑起来看缺口。新增只读结构化查询工具（safe 级，须登记
  `_SAFE_TOOL_ALLOWLIST`）：`list_chapters`（卷章+状态）、`get_bible(section, id?)`、
  `get_outline(vol, ch)`（细纲）、`list_conflicts`（待裁决）。理由：现在 agent 只能靠
  read_file/grep 猜 JSON 结构，脆且费 token；结构化查询消除"文件布局幻觉"。
  **刻意不给 CLI 直通工具（受控 shell）**——那会让门禁形同虚设。
- **D · 会话历史持久化到项目**：落 `<proj>/workspace/agent/session.jsonl`（ADR-016 文件即
  持久事实源，不入 git）；重开 console 自动续接。**书切换对齐**：console 内 `/open <id>`
  切项目时在 jsonl 写段标记（`{"type":"switch","from":..,"to":..,"at":..}`），
  回放只取当前项目之后的段落，不把上本书的对话混进新上下文。
  **上下文裁剪**：回放按消息字符预算取**最近窗口** + 保留首条项目快照；与 AgentRunner
  的观测预算同哲学（超预算即截断而非无限累积）。
- **E · 缓存纪律（DeepSeek 前缀缓存，价差 50 倍）**：system prompt **只放静态规则**
  （角色、边界、工具用法、输出风格）；项目状态（当前书、已写章节、待裁决冲突）由
  首条 user 消息的快照给一次 + agent 用工具按需读。system prompt 绝不放逐章变化内容。
  新 prompt 文件入库后走既有离线 dump 审计流程（`build_system_prompt` 同款手法）。
- **F · 成本可见**：REPL 每轮显示本轮 token/成本（AG-12 记账已有）；`Budget.max_rounds`
  硬顶 + converge 兜底（AG-13）沿用。`--smoke` 等价物：agent 循环用 fake provider 即可
  全离线测（`AgentRunner` 决策者可注入）。
- **G · 命令形态**：`novelist console` 为唯一入口；`/help` 增补自然语说明与
  `/agent status`（看本轮会话的调用数/成本/当前书）。**不做**独立的 `novelist agent` 命令。

**实现规划**：见 docs/08 **M3ac**（自然语通道 → 结构化查询工具 → 会话持久化 → prompt 与审计 →
真机验收并入批次 D）。

**落地记录（2026-09-19 同日）**：M3ac-1…4 已实现——`core/agent_chat.py`（ChatAgent +
SYSTEM_PROMPT + 快照/持久化）、`AgentRunner.run_chat` + `load_messages/export_messages`、
`tools/query.py` 5 个只读查询工具（双写 EVIDENCE_TOOL_NAMES 与 G3 白名单）、console
非 `/` 通道 + `/agent status` + `_io_decision` 审批通道。测试 17 例（test_agent_chat +
test_query_tools）。与 ADR 的一处偏差：会话按**项目分文件**存储，天然隔离，
"切书段标记"失去必要（更简单的严格隔离），回放 = 该项目的最近窗口。

**首轮真机复盘（2026-09-19，proj-20260919115034）**：FC 判据**已通过**（raw-calls 出现
tools 下发与 get_bible/list_chapters 等真实 tool_calls 往返）；同时暴露并修复：
①**观测预算跨轮泄漏**（`_obs_chars` 不重置，turn2 烧光 32k 后 turn3 工具全废）→
`run_chat` 轮首归零 + 对话态放宽到 48k；②**无结构化写通道**（agent 反复 read_file
52KB 蓝图打转、两轮零落地）→ 新增 `update_blueprint`（sensitive，按段按字段合并写 +
自动 sync_bible）+ `get_bible` 覆盖 `blueprint` 段；③对话轮零可见性 → trace_tools
工具播报 + 轮首「思考中」+ 心跳；④chat `max_tokens_out` 4000→8000（finish=length
空输出）；⑤ChatAgent 挂 `chat:<pid>` calllog 锚点。测试 +6（test_agent_chat_ops）。

**风险**：FC 从未真机验证（raw-calls 停在 09-16）——若 DeepSeek `deepseek-v4-flash` 的
`tool_calling` 实际不可用，AG-20 能力门控会让自然语通道退化为"单轮问答 + 无工具"，
届时需另拍降级方案（伪工具协议或换 provider）。

## 6. 与其他备选方案的对比小结

| 备选 | 为何不选 |
| --- | --- |
| 单次/分块全文生成（无 Agent） | 无全局一致性、无工序、无返工，无法支撑连载长文 |
| 纯对等群聊 MAS（如 AutoGen 群聊） | 不可控、成本爆炸、易漂移，无法门禁质量（M4 最低分） |
| 纯流水线（无编排者） | 灵活性不足，无法应对回流、伏笔逾期、人工跳步等动态场景 |
| 纯黑板（无编排者） | 缺仲裁，易相互覆盖，无明确负责人（M3 次低） |
| 采用重框架（AutoGen/LangGraph/CrewAI 全盘接手） | 抽象过度、生态锁定、深定制难；项目需求独特且后续要深度打磨创作语义 |

> 权衡取舍：本项目以**可控与一致**为第一目标（网文商业化最看重"书不崩、设定不乱"），而**可扩展性**通过插拔 Provider 与规约化文件结构来获得，而非引入重量级框架。
