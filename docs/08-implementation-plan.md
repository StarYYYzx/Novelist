# 08 · 工程实现规划

> 怎么把前面的设计落地。技术栈、代码结构、里程碑路线图、风险清单。**本文是"何时做、怎么做"，答案已是设计产物。**

## 1. 技术栈选择

| 层 | 选择 | 理由 |
| --- | --- | --- |
| 语言 | Python 3.11+（当前环境 3.14 亦兼容） | Agent/AI 生态成熟、express 循环易写 |
| 异步 | asyncio + 进程池 | 单进程内并发子代理/围读演员；**正文编写严格串行** |
| 类型/校验 | Pydantic v2 | JSON Schema 契约直接映射校验 |
| Agent 循环 | **自研轻量内核**（不绑死框架） | 设计独特、需深度定制；可隔离依赖 |
| LLM 接口 | 自研 Provider 抽象 + 各 SDK 可选依赖 | ADR-003 插件化 |
| 本地推理 | Ollama / vLLM（OpenAI 协议） | 满足本地部署需求 |
| 配置 | TOML + 环境变量 | 简单、版本可控 |
| 存储 | 文件系统工作区 + 轻量 JSON + SQLite 辅助索引 | ADR-004/010/016，兼容 Git + 可并发读范围查询 |
| 日志 | structlog / logging | 事件化、结构化 |
| 测试 | pytest + pytest-asyncio | 标准 |
| 对外 | click(CLI) + FastAPI(HTTP) | 轻量、成熟 |
| 敏感词预检 | 内置中文敏感词表（可插拔） | ADR-015 上游预检降低审核命中 |
| Embedding | 随 Provider 提供（云/本地）或回退关键词 | 记忆/围读会检索 |
| 围读会会话 | asyncio 内存场景总线（scene） | 受控群聊（ADR-014）；场景级状态进程内管理 |

> **不引入**重量级多 Agent 框架作为内核依赖（决定见 03§6），仅可选用其思想；避免抽象过度与锁定。

## 2. 代码目录结构

```
novelist/
├── pyproject.toml
├── src/novelist/
│   ├── __init__.py
│   ├── core/               # 与表层无关的内核
│   │   ├── agent_runner.py     # Agent 循环执行器（主编剧 & 子代理共用）
│   │   ├── subagent.py         # 派发/回收/隔离会话管理（含演员派发）
│   │   ├── orchestrator.py     # 主编剧具体实现（守则 + 决策）
│   │   ├── pipeline.py         # 工序状态机（串行逐章；事件回写状态）
│   │   ├── permission.py       # 门禁与策略
│   │   ├── budget.py           # token/成本预算
│   │   ├── memory.py           # 记忆子系统编辑/索引入口
│   │   ├── scene.py            # 围读会（受控群聊）场景总线（ADR-014）
│   │   ├── moderation.py       # 审核拦截识别 + 降级链（ADR-015）
│   │   └── events.py           # 事件总线/审计
│   ├── memory/             # 记忆子系统（ADR-011）
│   │   ├── retriever.py        # query_memory / 检索（语义+关键词兜底）
│   │   ├── chronicler.py       # 编纂：提炼/写入/冲突校验（ADR-013）
│   │   ├── validators.py       # 记忆冲突双层校验器
│   │   └── index.py            # RAG 索引构建/增量/落盘
│   ├── tools/              # 工具注册表 + 各工具实现
│   │   ├── registry.py
│   │   ├── filesys.py      # read_file/write_file/list_dir/grep
│   │   ├── writing.py      # write_draft/promote_draft
│   │   ├── setting.py      # update_entity/add_plot_thread/set_timeline
│   │   ├── outline.py
│   │   ├── memory.py       # query_memory / append_experience / append_plot_event / record_relationship_change / reindex_memory
│   │   ├── takes.py        # write_take（角色演员试演）
│   │   ├── scene_tools.py  # join_scene / say_line / leave_scene / close_scene（围读会）
│   │   ├── consistency.py  # run_rule_check/run_semantic_check
│   │   └── governance.py   # delete/batch_rewrite/checkpoint/publish
│   ├── providers/          # LLM Provider 适配器(插件)
│   │   ├── base.py         # LLMProvider / Embedding 抽象
│   │   ├── registry.py
│   │   ├── openai.py
│   │   ├── deepseek.py
│   │   ├── anthropic.py
│   │   ├── ollama.py
│   │   └── vllm.py
│   ├── consistency/        # 一致性规则引擎 + 语义检
│   │   ├── rules.py        # 确定性规则
│   │   └── semantic.py
│   ├── storage/            # 工作区读写、检查点、schema 校验、SQLite 索引
│   │   ├── workspace.py
│   │   ├── checkpoint.py
│   │   ├── indexdb.py      # .index.db 读写/重建（ADR-016）
│   │   └── schemas/        # 实体 JSON Schema（见仓库根 schemas/）
│   ├── cli.py              # click 命令行
│   ├── server.py           # FastAPI HTTP
│   └── config.py           # pyproject+toml 配置加载、policy 解析
├── agents/                 # 各 Agent 系统提示（提示词独立成文本文件）
│   ├── orchestrator.md
│   ├── worldbuilder.md
│   ├── outliner.md
│   ├── wordsmith.md
│   ├── reviewer.md
│   ├── plotkeeper.md
│   ├── chronicler.md       # 记忆编纂员
│   ├── actor.template.md   # 角色演员提示词模板（{character}/{history}/{scene} 占位）
│   └── inspector.md
├── schemas/                # 实体 JSON Schema（bible/memory/outline/config/policy/llm/events/tools）
│   ├── bible/{worldview,characters,locations,timeline,plot_threads,style}.schema.json
│   ├── outline/{volume,chapter_gist}.schema.json
│   ├── memory/{character_history,plot_event,relationship,fragment_index}.schema.json
│   ├── config.schema.json
│   └── policy.schema.json
├── tests/                  # pytest 冒烟 + 契约校验测试
└── docs/
```

## 3. 里程碑路线图

### M0 — 骨架与契约（1 周）✅ 已完成
- 建立仓库、pyproject、CI 骨架。
- 落地 `core/agent_runner.py` 最小循环 + `providers/base.py` 抽象 + 事件总线。
- 落定 `schemas/*` 实体 JSON Schema 初版 + 工作区目录解析器。
- 冒烟用例：一次 `complete()` + 一次工具调用。
- `init` 命令真实化：创建项目工作区 + project.json + .checksum.json。
- 实现：`storage/workspace.py`（目录规约/沙箱/原子写）、`storage/checkpoint.py`（快照/校验/恢复）、`storage/models.py`（SchemaRegistry + Pydantic 实体）、`providers/openai.py`（OpenAI 兼容适配器 + 拦截识别）。**27 个测试全绿**。

### M1 — Agent 循环闭环（2 周）✅ 已完成
- 主编剧可按剧本自主调用**只读 + 写作**工具完成"生成一章草稿"循环。
- 工具注册表 + 门禁（safe/sensitive 先行）接入。
- 子代理派发/回收可用（至少 1 个：文字匠）。
- 检查点初版（快照 + 恢复，已在 M0 完成）。
- 实现：`tools/`（read_file/write_file/grep_text/write_draft/promote_draft/query_memory/get_character_history + `build_registry` 装配）、`core/agent_runner.py`（LLM 驱动多轮循环 + tool_calls 解析 + 预算收敛）、`core/orchestrator.py`（`produce_chapter`：主编剧经循环调 write_draft 落盘草稿 + 事件实时回写 commit_event）、`providers/fake.py::ScriptedProvider`（脚本驱动循环测试）、CLI `chapter` 命令。**34 个测试全绿**（含一章生产 + CLI 端到端）。

### M2 — 流水线与一致性（2 周）✅ 已完成
- 工序状态机完整贯通（大纲→细纲→正文→审查→修订）：CLI `run` 真实推进状态机并写回 `project.json`。
- 一致性规则引擎（引用完整性、时间线）✅ 落地：`consistency/rules.py`（`run_rule_checks`，输出结构化告警 `level/rule_id/object_ref/detail`），CLI `run` 在审查阶段接入并汇总告警数。
- 审校师（LLM 语义检）接入并合并告警：语义检随 M3 记忆检索/编纂完整化一并落地（当前由规则引擎 + 事件回写契约校验兜底）。
- 串行逐章生成（一章完成→事件实时回写→下一章）+ 预算控制（NFR-1/9/14）✅（M1 起）。
- **记忆子系统初版** ✅：`core/writeback.py` `commit_event` 真实写入 `memory/`（人物经历/剧情事件）+ 契约校验（引用完整性），关键词检索 `query_memory` 降级可用；冲突双检/语义检索随 M3 完整化。
- **DeepSeek 真实适配器** ✅：`providers/deepseek.py`（`DeepSeek-API-KEY` 环境变量），真实 API 集成测试 `test_deepseek_live_completion` 通过。

### M3 — 治理与交付（进行中）

#### M3a — 门禁与交付 ✅ 已完成
- danger 级门禁流程完整（CLI `grant` 审批 + 策略文件 `PermissionGate.from_policy_file`）。
- CLI 全命令可用；`novelist export` 发布包 + `stats` 统计。
- 治理工具：`publish` / `delete_file` / `checkpoint`（danger 默认 deny，可审批放行）。

#### M3c — HTTP 服务 ✅ 已完成
- FastAPI（docs/07 §6.2，F8.3）：项目列表/状态、流水线推进、串行写章、导出、待决审批与决策。
- 审批队列跨进程复用：HTTP 与 CLI `grant` 读写同一份 `logs/pending_approvals.json`。
- `novelist server --host/--port`；7 个端点测试全绿。

#### M3d — 记忆子系统完整化 ✅ 已完成
- **Embedding 与降级（F9.4）**：`core/embedding.py` 提供 `KeywordEmbedding`（中文二元组 + IDF 加权精确打分）
  与 `OpenAIEmbedding`（OpenAI 兼容 `/embeddings`）；`make_embedding()` 在无 key / 缺 httpx 时自动降级，
  检索接口不变、结果仍可用。
- **检索（docs/07 §7.1）**：`core/memory.py` 的 `MemoryRetriever` 双路径——
  语义模式走向量余弦，关键词模式走**精确 token + IDF 打分**。
  （刻意不用定长哈希向量做余弦：小语料下碰撞噪声会盖过真实信号——实测 dim=256 时"零重合"文档能得 0.16 分，
  高于真实命中的 0.12；精确集合运算无碰撞，零重合即 0。）
- **索引（ADR-016）**：`memory/fragment_index.json`（元数据 + hash，供去重与冲突定位）
  \+ `memory/rag/vectors.json`（**可再生缓存**；删掉后检索回落到即时计算或精确打分，正确性不受影响）。
- **冲突双检（F11.3）**：`MemoryWriter` 写前校验——规则层（重复入库、bible 引用完整性 `char:`/`pt:`/`loc:`）
  \+ 语义层（经 `semantic_checker` 回调注入，由编纂员子代理承担）；冲突抛 `MemoryConflictError`、
  回写层转为 `ContradictionError` 并回退，**不静默入库**（docs/06 §4.4 `conflicted` → 人工仲裁）。
- **事件实时回写接入（A9）**：`commit_event` 改为经 `MemoryWriter` 落记忆并**增量更新索引**，
  下一事件/下一章立即可"先忆"（F11.4/F11.5）。
- **先忆升级（F3.4）**：CLI `chapter` 的 goal 组装由"取 plot_events 末尾 3 条"改为**按相关度检索召回**
  （依细纲语义召回，而非按时间顺序）。
- **工具**：`query_memory`（支持 char_id / kinds / 章节范围过滤）、`get_character_history`、
  `get_plot_events`、`reindex_memory`（sensitive，走门禁）。
- 测试：`tests/test_m3_memory.py` 22 个用例（降级、排序、过滤、冲突双检、索引可再生、端到端闭环）。

#### M3e — 成稿质量增强 ✅ 已完成

> 针对端到端系统测试（`novel_workspace/_harness/系统整体测试报告.md`）暴露的缺陷逐项修复。
> 编号沿用报告里的 B-xx。

- **B-02 圣经注入** ✅ 新增 `core/context.py`。按章节装配 世界观 / 境界体系 / 世界铁律 /
  文风（视角·时态·笔调·禁用词·专有名词）/ **本章出场人物卡（含性别·境界·阵营·性格·弧线）** /
  主角代词硬约束 / 未回收伏笔 / 输出纪律（元叙事、完整性、人物边界、事实一致）。
  `produce_chapter(inject_bible=True)` 默认开启。
  *出场人物取"已登场且仍在场"而非"细纲点名"*——只按细纲点名会让模型忘记既有角色，
  转而在正文里另造名字填坑，反而加剧凭空造人。
- **B-03 编纂员** ✅ 新增 `core/chronicler.py`。LLM 从成章正文抽取真实情节事件 →
  `MemoryWriter` 冲突双检 → 写 `plot_events` 与各人物经历。
  章级合成事件改为**自动兜底**：有真实事件就不写，编纂不可用才写（`commit_chapter_event=None`）。
- **B-04 生成完整性校验与重试** ✅ `core/polish.py::completeness`：截断、元叙事泄漏、
  篇幅检测；`produce_chapter(validate=True, max_retries=1)` 带修复提示重生成。
  *元叙事检测跳过首行*——章节标题本来就该写「第X章」，真正的泄漏是叙述里的说法。
- **B-05 CLI 生成预算可配** ✅ `novelist chapter --gen-tokens N`（原对 lmstudio 硬编码 400）。
- **B-06 CLI promote 命令** ✅ `novelist promote --vol/--ch | --all [--policy]`（sensitive，
  默认 ask 门禁，可交互审批或策略放行）。CLI 现共 10 个命令。
- **B-07 记忆回退与修订** ✅ `MemoryWriter.drop_by_source(vol, ch)` 与 `revise_fragment(sig, text)`，
  模块级 `rollback_chapter(ws, pid, vol, ch)`。章节重写时新旧事件不再并存。
- **B-08 审校（双层）** ✅ 确定性层：`rules.py` 新增 **R-LEX**（现代词 / 西方典故 / style 禁用词）
  与 **R-PWR**（同一境界混用「层」「重」等细分表述）。语义层：新增 `consistency/reviewer.py`
  审校师（设定矛盾 / 人设漂移 / 称谓失当 / 时间线 / 战力越级 / 事实前后矛盾 / 细纲未覆盖），
  `run_consistency(ws, pid, llm=...)` 追加为 `R-SEM` 告警；CLI `novelist review --provider ...`。
- **B-09 敏感词表** ✅ `core/moderation.py` 内置起步词表（违禁品 / 赌博 / 极端暴力 / 违规导流），
  支持 `.txt`/`.json` 词表文件与 `bible/moderation.json`、`NOVELIST_BANNED_WORDS` 环境变量。
  ⚠️ 起步清单**不构成本地合规词表**，生产环境须加载完整词表或对接专业审核服务。
- **B-13 导出书名** ✅ `export_project` 取 `project.json.title`，回退 `project_id`。
- **文风优化（新增）** ✅ `core/polish.py`：九种可测的"AI 味"信号（对比排比、模糊比喻、
  「X 如 Y」模板、时间套话、认知动词开头、破折号/省略号、形容词堆叠、结尾升华、段落节奏过匀），
  给出 0–100 的确定性分数；`polish_chapter()` 在成章后**额外追加一次 LLM 调用**定向改写，
  并用同一指标复核——**分数没变好或章节被写坏就保留原稿**。CLI `chapter --polish`。
- 测试：`tests/test_m4_quality.py` 40 个用例。全量 135 passed。

#### M3f — 事件循环 / 世界状态层 / 续写 / 剧本草稿 ✅ 已完成

> 人工审查第二批第 1、2 条与第三批第 2、3 条的落地。

- **事件循环（第二批第 2 条 / ADR-013 真落地）** ✅ `produce_chapter(event_loop=True)`：
  按细纲 `key_events` 声明式清单逐事件生成——每事件带**上文接缝**（上一事件末尾 300 字）
  \+ **事件级先忆**（本地检索，不花 LLM 调用）；每个事件写完立即由编纂员回写
  （含冲突双检），章内后续事件可先忆到上一事件。`key_events` 缺失时回退整章生成。
- **续写（第二批第 1 条·第 2 层）** ✅ `finish_reason=="length"` 时把已有文本作前缀续写，
  不再整章重来（省思考开销、保住已写好内容）。直出路径默认启用。
- **剧本草稿（第三批第 2 条·档 2）** ✅ `produce_chapter(screenplay=True)`：重场戏先以
  剧本体写对白交锋，再叙事化（保留对白原话、补动作场景心理）。成本 ×2、无失控风险。
- **世界状态层（第三批第 3 条）** ✅ 新增 `bible/worldstate.json`（schema 见 schemas/）：
  人物**当前**修为/位置/持有物/伤势的确定性事实源。编纂员从正文抽取 `状态：` 行并在
  事件回写时同步更新（`state_delta` 字段启用）；生成上下文注入【人物当前状态】，
  战力对比以此为硬约束。
- **R-STATE 规则** ✅ `consistency/rules.py`：境界只进不退（倒退 → block）、一次跨 ≥2
  大境界（→ warn，须有突破描写）、已死亡人物死亡章后仍出场（→ warn）。纳入
  `run_rule_checks` 全量入口。
- CLI：`chapter --event-loop / --screenplay`；测试：`tests/test_m5_adv.py` 17 个用例。
  全量 157 passed。

#### M3g — 注册表体系 / 设定条目库 / 分级检索 / 类型适配 ✅ 已完成

> 第三轮讨论落地（物品/功法注册表、首次交代状态机、记忆分级、跨类型适配）。

- **物品/功法注册表（讨论·游戏式资产登记）** ✅ `schemas/bible/items.schema.json` +
  `skills.schema.json` + `core/registry.py`：唯一 id（`item:`/`skill:`）+ 规范名 + 别名表。
  `canonical()` 把「残篇/残卷/忘情录」归一化为规范名——物品是最后一种没有"唯一身份"
  的实体，注册表从根上消除同物异名（奖励倒退的直接根源）。
- **设定条目库 + 首次交代状态机（讨论·世界观缺失）** ✅ `schemas/bible/settings.schema.json`
  + `core/settings.py`：世界观按独立知识单元切块（境界体系/势力…），每条 `keywords[] +
  revealed`。生成前**提及检测**（事件文本命中关键词）→ 只注入**未交代**条目到事件 prompt
  （"首次出现，须在正文自然带出"）；章末**交代验证**（正文关键词扫描）→ 命中置
  `revealed=true` 写回，未命中保留 false 下章继续注入。注入走 prompt 引导而非代码插入
  （避免说明书腔），确定性由验证闭环保证（首次交代是硬状态，不是模型自觉）。
- **分级检索（讨论·角色分级记忆）** ✅ `core/memory.py`：plot_event 碎片带事件 type，
  检索按类型加权（turning_point/reveal ×1.30、conflict ×1.10、dialogue ×0.80）——
  "先忆"天然偏重关键情节而非流水账。长期记忆仍"只存不注入"，人物卡核心字段代表。
- **跨类型适配（讨论·都市高武）** ✅ `core/worldstate.py`：`_STATE_KEYS` 扩展
  实力/战力/等级 → realm。境界层级本就由 worldview.power_system.levels 配置驱动，
  换类型只需重写 bible（世界观/人物/细纲），系统代码零改动。
- **R-ITEM 规则** ✅ `consistency/rules.py`：正文同一物品多种叫法 → warn；正文出现
  注册表物品但 worldstate 无人持有 → warn。子串陷阱：先剔除规范名出现再查别名
  （「忘情录」⊂「太上忘情录」直接匹配会误报）。
- 测试：`tests/test_m6_registry.py` 10 个用例。全量 167 passed。

#### M3h — 明暗线闭环 / 审校闭环 / 人物首现提示 ✅ 已完成

> 讨论第 6、7 轮落地（首次介绍/明暗线/递归评估/审校处理链路）。

- **明线（第 6 轮）** ✅ `context.py`：`build_chapter_context` 读 `outline/volumes.json`，
  把当前卷 `summary` 注入 system prompt【本卷主线】——每章须服务主线而非只有细纲要点
  （此前卷主线从不进生成上下文，主线靠细纲人工对齐）。
- **暗线（第 6 轮）** ✅ `chronicler.py`：`_link_threads` 用伏笔 desc 关键词（token 交集）
  匹配事件摘要，填充 `affected_threads`，并把 `planted` 伏笔推进为 `active`（写回
  `plot_threads.json`）。plot_threads 从"登记+注入提醒"装上闭环；事件↔伏笔有数据关联。
- **审校闭环（第 7 轮）** ✅ `orchestrator.py`：事件循环里每事件生成后调 `Reviewer`
  审校该片段，block 级问题**带审校建议重写该事件**（限 1 次，只修订不整章重来——
  此前审校是"只记录不处理"，`ReviewIssue.suggestion` 从未被使用）；block 沉淀到
  `bible/review_lessons.json`（项目级独立文件，去重、上限 30 条），后续生成注入
  【历史教训】段——问题不重复发生（经验回灌）。
- **人物首次出场提示（第 6 轮）** ✅ `context.py`：`first_appear == 本章` 的人物卡标注
  「本章首次出场：通过行动/对白自然认识，不写成人物简介」——人物介绍靠行动带出
  （硬交代会变说明书腔），组织/设定走 settings 状态机。
- **深度优先递归（第 6 轮评估）**：**未实装**——采用声明式 key_events（第二批第 2 条
  决策）。评估结论：3–5 章规模声明式够用；重场戏想写厚时可做"受控一层递归"
  （细纲标记可展开 → 一次 LLM 子事件清单 ≤3 个），优先级低于前四项。
- 测试：`tests/test_m7_plotline.py` 7 个用例。全量 173 passed。

#### M3i — RAG 知识检索层（prompt 注入检索化 + 向量化 + LLM 查询生成）✅ 已完成

> 讨论第 8 轮（用户拍板三件都做）：把 system_prompt 里 5 块"静态全量/固定前 N"
> 检索化，长卷不随内容膨胀，且相关性注入 = 注意力不被稀释。

- **KnowledgeBase（`core/knowledge.py`）** ✅ 统一知识条目：设定/人物（含 worldstate
  状态行）/伏笔/教训/势力/物品功法，从 bible 各文件收集为可检索条目。
- **三层检索** ✅ ① 确定性：关键词 + token 交集（与 MemoryRetriever 同口径，兜底）；
  ② 语义：向量化后余弦（nomic-embed，`make_embedding("openai", base_url=LM-Studio)`，
  keyword 模式自动退化）；③ **LLM 查询生成**：`plan_queries` 让模型决定"本事件需要
  哪些知识"→ 用生成的查询检索（`knowledge_llm=False` 退化为事件文本检索）。
- **system 瘦身为 L1 基座** ✅ 出场人物全卡（≤16 人）→【人物名单】名字+境界一行
  （防造人）；势力前 6 全给 → 事件级命中注入；伏笔/教训固定前 N → 事件级相关检索；
  人物当前状态 → 随相关人物卡事件级注入（战力硬约束不丢）。保留：身份/主线/铁律/
  境界体系/文风/主角约束/纪律/格式。
- **事件级注入** ✅ `_event_goal` 增 related 段：相关人物卡（带当前状态/首现标记）、
  相关设定/伏笔/教训/势力——知识检索 → 过滤 → 注入一条链。
- **最近 1 章固定回退** ✅ 相关检索召回语义相似但未必时间最近，长卷下直接带最近一章
  事件补连续性（用户拍板）。
- 测试：`tests/test_m8_rag.py` 7 个用例。全量 180 passed。

#### 待办
- ~~v5 阶段修复~~ **已修**（提交 181de3c，见 人工审查 第七批）：P0-1 R-STATE 绑定豁免、
  P0-2 编纂能力词过滤 + NOOP 扩展、P1-1 R-LEX `modern_words_exempt` 豁免表。
- **世界观 skill（M3j）→ 并入 M3l 构建层 Forge**：类型包 Genre Pack（修仙男频/通用，数据驱动）+
  递归 LLM 生成（世界观 → 人物 → 卷主线 → 细纲，每步由模型判断是否需要细化）+ 人工 review 定稿
  ——补 B-01 的世界观构建师/大纲师。设计见 `docs/10-forge.md`，不引入 subagent 框架。
  **JIT 补卡子代理已先行落地**（递归分层 A，`orchestrator._jit_characters`），Genre Pack 随 F0/F4 做。
- ~~递归分层深化~~ **已落地（M3k，提交 181de3c）**：卷→章（人物 JIT `出场人物:` 声明 →
  生成前补卡）→ 事件 → 拍（细纲事件标 `[expanded]` → ≤3 拍逐拍生成，失败回退事件级）；
  章→事件层"世界观补充"细化点（`supplement_settings`，滚动 settings，revealed=False
  由首次交代状态机接管）；深度 ≤1、宽度 ≤3、检查点兜底。回读机制（前 1 章正文原文注入）
  与事件级润色（tone 驱动风格 skill）同批落地。
- **篇幅硬上限**：事件 prompt 加输出上限（ch3 7500 字 vs 目标 2400）——拍展开已缓解，
  事件级仍有膨胀可能，待评阅数据确认。
- **角色演员**：actor 提示词模板 + `write_take` 工具 + 多角"排演→整合"流程（ADR-012）。
  `core/scene.py`（SceneBus）与 `core/scene_tools.py`（join/say/leave）已就绪，**尚未接入编排流**。
- **受控围读会**：结束判据（全体离场 / 轮次上限 / 收敛 / 超时）与主持人调度尚未接到 `produce_chapter`（ADR-014）。
- **子代理框架**：`core/subagent.py` 仍不存在。当前 B-03 编纂员与 B-08 审校师是**独立的领域组件**，
  由编排层直接调用，尚未统一到 docs/05 §3 的 `SubagentTask`/`SubagentResult` 契约与隔离会话机制。
- **冲突双检有效性**：真实语料上一次都没触发，需专门构造用例验证（F11.3）。
- **大纲覆盖度检查**：docs/05 的「检查员」未实现；实测 22 建档人物中 3 人正文零出场且无告警。
- **审计日志完整**（F7.1）：事件与 token 计量尚未落 `reports/` 与 `.index.db`。
- **围读会（档 3）**：SceneBus 已就绪，但按 ADR-014 接入编排流（主持人 + 结束判据）未做；
  档 2（剧本草稿）已先行落地，围读会定位为"探索 + 对白素材"，产出不直接进正文。

### M3l — 构建层（Forge）：立项→世界观→大纲→细纲 的真实产出（2026-09-01 立项）

> 设计文档：`docs/10-forge.md`；ADR-017（Blueprint 中间态 + 双模式收敛）、
> ADR-018（构建期递归深化：模型自判 + 引擎硬边界）。

**为什么单列**：前四道工序此前是空转（`cli.py:87-99` 只推进状态字符串，bible 全库只有读没有写），
A1 不满足。本里程碑把"人手写 bible + 细纲"这一步自动化，是补齐 A1 的唯一路径，
同时取代原"世界观 skill（M3j）"待办里的模板库与分步生成部分（世界观构建师/大纲师职能由 Forge 承担，
**不引入 subagent 框架**，用户拍板）。

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| F0' | **schema 对齐（前置）**：`schemas/file/*.schema.json` 文件层 + 4 处字段差异修复 + `schemas/forge/blueprint.schema.json` + `genres.schema.json`（蓝图/类型包自身可校验） | ✅ 完成（契约校验层 core/bible.py + CLI validate + 5 项目全过，见下） |
| F0 | `state.py`（Blueprint/provenance）+ `slots.py`（槽位与缺口检测）+ Genre Pack 装载 + `forge show` | 待做 |
| F1 | 模式一全权：seed 提炼 + 授权询问 + 递归引擎最小树（book→volume→chapter，**卷闸门 vol=1**）+ 落盘（含 worldstate 确定性合成）+ provenance 保护 | 待做 |
| F2 | 商讨：分轮分组问答 + 候选批量生成 + transcript 续跑 + 非 TTY 降级 + 自由答案 | 待做 |
| F3 | 模式二 ingest：切章预览/抽取（超限降级确定性）/消歧/文风画像/卷章编码/记忆初始化/实体 warm-up + 缺口回落商讨 | 待做 |
| F4 | 递归深化：worldview/character/style/threads 旁支节点 + 可选 arc/beat 层 + `forge roll` 滚动生成 + Genre Pack 扩充 | 待做 |
| F5 | `validate.py` V1–V6（含 FakeProvider 可写冒烟）+ report 双写 + rollback/--diff + pipeline 推进到「细纲」 | 待做 |

验收：AG1 一句话 → 项目可直接 `chapter 1 1` 且 `bible_injected=True`；AG2 3 章样章 → 第 4 章起可接写
且记忆非空；AG3 商讨 12 问内收敛；AG4 构建调用数不超分阶段配额（build 卷 1 = 60 / roll 每卷 = 40 /
ingest = 30）。设计三轮敲定（2026-09-01），29 项分支决策见 `docs/10` §15。
**执行顺序**：本里程碑在 **M3m（时间线 T1–T3）之后**启动——F3 的 ingest 依赖 chronicler
时间行（T1 产物），先接泵再摄入。

**F0' 落地记录（2026-09-01）**：契约对齐分三层——schema / 磁盘 / 校验入口，互相校准后 5 项目全过：

- **schema 根节点修正（结构层）**：characters/locations/items/settings/plot_threads/timeline/
  outline-volume/memory-plot_event 8 个根节点从 object 改 **array+items**（磁盘实然即数组，
  docs/06 §3.1/§3.3.1 明示）；fragment_index 相反，根从 array 改 **object**（磁盘
  `{revision,kind,dim,fragments}`，`MemoryIndex.load` 读 `raw["fragments"]`）。
- **字段契约校准（实然消费者）**：style 废弃错误的顶层 `style:string`，重构为扁平
  （pov/tone 数组/target_words_per_chapter/forbidden_words/protagonist——context.py:69、
  rules.py:66 读取）；worldview 补 power_system/phase_policy/unavailable_states/factions
  （phase.py:122 / worldstate.py:79 / knowledge.py:131 消费）；characters items 补
  background/is_protagonist 且 additionalProperties 放宽（人物卡为示意结构，允许 possessions 等扩展）。
- **历史兼容（旧数据不破坏）**：timeline.at 用 **oneOf**（新 `t/vol/ch` | 旧历法
  `era/year/season`，R-TL 已回退 in_chapters 章序）；plot_event enum 对齐
  `chronicler.EVENT_KINDS` 六类 + id 允许 `ev:proj:vol:ch:seq`、affected_threads 兼容
  `thread:` 前缀；character_history state_delta 允许 null、char_id 兼容 `char_` 旧前缀；
  project id 模式放宽（project_id 即目录名 `proj-t5` 无冒号）。
- **磁盘数据补齐**：5 项目 worldview 补 `id`（world:luoxia 等）、缺失 title 的 project.json 补书名、
  proj-t5 items 补 type、characters 补 status（schema 已放宽 required，补全以保语义）。
- **校验入口接通（B1 阻塞解除）**：新 `core/bible.py`——`BIBLE_CONTRACT` 14 类映射
  （含 `memory/character_histories/*.json` glob）+ `validate_project()` 返回违规清单
  （缺失文件不违规，加载层空值兜底）+ `parse_gist()`（细纲 front-matter 宽松解析，
  兼容旧行内 `key_events: [...]`，供 F1 Forge 使用）；CLI 新增 `novelist validate`（项目直传
  或 workspace 根全查）。`SchemaRegistry.validate` 从零调用点变为 Forge 前置门禁。
- 测试：`tests/test_m11_schema_alignment.py` 31 用例（契约映射完整性 / 16 组样例全 PASS /
  validate_project 行为 / parse_gist 三形态 / proj-t5 集成 skipif）；存量 test_m0 样例适配数组根。
  全量 293 passed；`novelist validate novel_workspace` 5/5 项目全过。

### M3m — 时间线与定时事件（ADR-019，2026-09-01 拍板）

> 设计：`docs/06` §3.3（含数据结构 / 抽取行 / 分档表）。用户提议、四项分支拍板
> （天数轴 / 分档加压+告警 / worldstate 内嵌 / 状态约束本批一起做）。

**为什么单列**：时间维度"壳存在、泵未接"——`timeline.json` 实跑为空、`LandedEvent.timeline_delta`
零调用方、R-TL 空转。本里程碑把事件级时间推进、未来日程登记、到期渐进提醒、不可出场约束四件事
一次接通，全部搭编纂员既有调用与确定性规则，**零新增 LLM 调用**。

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| T1 | `worldstate.json` 扩展（time/pending/unavailable_until + history.at 加 t）+ chronicler "时间：/约定："行抽取 + `timeline.json` 写入激活（量词归一、闪回/同日、dt 上限告警）+ **worldstate/timeline schema 同步修订**（AGENTS.md 约定：改 schema 须同步 docs/06）+ Genre Pack `unavailable_states` 词表 | ✅ 完成（core/timeline.py + chronicler Extraction + worldstate 扩展 + schema 同步） |
| T2 | 渐进提醒分档注入（produce_chapter「临近事项」段，仿 PhasePolicy；与 payoff_checklist 同通道）+ 软 block 拦截 + 章末记账（fired/expired） | ✅ 完成（orchestrator §1.6 注入 / §2.1 软 block / §5.5 tick，result.pending_tick） |
| T3 | R-TIME 新规则（到期 3 章 warn / 再 2 章 **软 block**：key_events 须引用该 pending / 连续 3 次 block 自动转 expired 放行 + report 留痕）+ R-TL 改按 t 单调 + R-STATE 不可出场告警 | ✅ 完成（rules.py R-TIME / R-TL at.t + 旧数据回退 / R-STATE unavailable） |
| T4 | Forge 联动：细纲 `key_events` 可选 `after_days` 登记 pending + ingest 抽取"三个月后"类约定 + build 末尾 worldstate 确定性合成（`time={now:0, origin_text}`，人物初始状态从蓝图合成，零 LLM）（随 M3l F3/F4 实施） | 待做 |

**T1–T3 落地记录（2026-09-01）**：

- `core/timeline.py` 新模块：天数解析（`+90日`/闪回/同日/量词归一，dt>3650 warn）、
  `advance`（推进+登记 timeline，id `tl:N`）、`add_pending`（due=now+dt，id `pd:N`，
  命中不可出场词自动打 `unavailable_until`）、`reminder_lines` 三档、
  `soft_block_check`、`tick`（兑现判定含**姓名+已到期兜底**——「闭关三月→出关」
  词面无共同二元组，见 §3.3 补记）、连续 block×3 自动 expired。
- chronicler：`Extraction` 具名容器（原二元组扩展为事件/状态/时间/约定四段），
  「时间：」「约定：」各最多 1 条，登记先于推进（due 按章首 now 算），
  状态历史 `at.t` 打章末时刻。
- orchestrator：`pending_tick` 进 ProductionResult；软 block 用**细纲原文**判定
  （不能用注入后的 goal——提醒文本会自我满足引用）。

验收：闭关事件登记 pending 且三个档位（30%/10%/到期）依次触发对应提示；闭关人物在不可出场期
出现在正文被 R-STATE 告警；`timeline.json` 非空且 R-TL 按 t 单调；全量测试不回归（262 passed）。

### M4 — 硬化与评测（持续）
- 完整评测集（见 09）与回归，含"记忆自洽 / 人设保真"专项（A7/A8）。
- 多个 Provider 实测（云 + 本地 Ollama/vLLM），含 Embedding 能力矩阵。
- 长文（≥20 章）全流程压力测试与一致性/记忆检索命中统计。
- **事件实时回写闭环**：事件落定即回写 memory + 下一书写点可先忆（NFR-13/A9）。
- **审核拦截降级**：MODERATION_BLOCKED 识别 + 改写/切换/人工链 + 敏感词预检（ADR-015/A10）。
- 稳定性/降级路径覆盖（含无 Embedding 时检索降级、审核拦截恢复）。

> 里程碑以"可演示的纵向切片"为单位（每次都有可跑通的新能力），而非单纯横向铺层。

## 4. 风险清单与缓解

| # | 风险 | 等级 | 缓解 |
| --- | --- | --- | --- |
| R1 | 主 Agent 上下文缓慢膨胀 | 高 | 04§5.3 压缩 + 引用式建模 + 记忆检索摘要化，先行落地 |
| R2 | 结构化输出不达标 / 模型漂移 | 高 | ADR-006 重试+降级 + 契约校验 + 检查员兜底 |
| R3 | 一致性门禁误杀（过度告警拖慢产出） | 中 | 规则分层 block/warn、可配置阈值、人工复核路径 |
| R4 | 写作/回写并发竞争导致脏写 | 中 | 正文严格串行 + 独立草稿文件 + 原子写 + 锁；事件回写与正文写隔离 |
| R5 | 成本失控（token 爆炸） | 中 | 预算上限阻断 + 并发节流 + 上下文压缩 + 检索剪枝 + 围读会轮次上限 |
| R6 | Provider 能力矩阵差异（降级差异大） | 中 | capabilities 探测 + 降级总表（07§8）+ 适配器白盒测试 |
| R7 | 提示词与实现耦合、难维护 | 中 | 提示词独立成 `agents/*.md`（含 actor 模板）+ 版本管理 + 评测驱动 |
| R8 | 沙箱/路径安全漏洞 | 高 | 严格白名单解析、路径穿越测试、禁止绝对路径越界 |
| R9 | 记忆检索命中差（"忆"不到关键史实） | 高 | 检索质量评测、命中率指标、多路召回（语义+关键词+卷序权重）、可调 top_k |
| R10 | 记忆污染（编纂幻觉/重复/矛盾入库） | 高 | 冲突双检 + 溯源定位 + 人工仲裁 + 可回滚（09§4） |
| R11 | 演员串味/越权读他人记忆 | 中 | actor 工具白名单 + take 路径沙箱 + 隔离会话 + 权限面收敛（05§7） |
| R12 | 围读会失控/不收敛（绕圈、越界） | 中 | 结束判据（轮次上限/全体离场/收敛判定）+ 主持人强制收场 + 场景隔离（ADR-014） |
| R13 | 厂商审核拦截导致产出停滞/缺章 | 高 | 拦截识别 + 改写/切换/人工链 + 敏感词预检 + 拦截统计监控（ADR-015/09§4） |

## 5. 实现顺序建议（TDD 取向）
1. 先 `storage`（工作区/schema/检查点）——是所有上层的地基。
2. 再 `providers`（抽象 + 1 个真适配器，含拦截识别）——打通 LLM。
3. 再 `core/agent_runner + tools/registry + permission`——打通 Agent 循环。
4. 再 `tools` 创作工具 + `pipeline`（串行逐章 + 事件回写）——打通一条完整工序。
5. 再 `memory`（读取→编纂→检索）——打通"先忆 / 后纂"，回填正文写作流程。
6. 再 `actor`（takes + 排演整合）→ `scene`（围读会 + 收敛判据）——打通人设生动化。
7. 再 `moderation`（预检 + 拦截降级链）——打通内容合规。
8. 最后 `consistency` 全量、HTTP、评测（含记忆/演员/围读/审核维度）。

## 6. 后续可迁移点（Backlog 对接）
- worker 进程池化（分布式）、多项目空间、文风学习、内容回流、跨书记忆迁移（对应 02§6）。
- 工程实现细化：`core/memory.py`（记忆子系统 + RAG 索引）、`agents/*.prompt`（含角色演员提示词）、`schemas/memory/*.schema.json` 的落地节奏见 M2/M3 的评测部分与本文 §3 路线节点的对应。

> 质量、评测、一致性保障的完整内容见独立文档 `docs/09-quality-assurance.md`。
