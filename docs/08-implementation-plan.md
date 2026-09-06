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
  *装配补读（2026-09-02，P0-1 残留收口）*：`power_system.note` / `worldview.summary` /
  `civilizations` / `systems` / `realm_fluctuates`（境界波动角色纪律行）与
  `worldview.modern_words`（现代词禁令）此前 forge 产出但装配层不读——扫描 6 个项目的
  `power_system.note` **6/6 全部静默丢失**，叶岚主线 `realm_fluctuates`/`modern_words` 0 条
  进上下文。补读后 schema 声明字段全有注入路径；约定：worldview 自定义附加字段不入顶层，
  写 `settings.json`（知识层可检索）。测试 `test_m4_quality` +2。
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
  *现代喻体词表（2026-09-02，v7 真机 ch4「指甲刮过黑板」收口）*：`worldview.modern_words`
  显式设空（穿越文放弃默认指称表）会连喻体防线一起关闭——喻体属**叙事修辞**，
  与穿越文合理指称（前世的手机/电脑）性质不同。新增 `MODERN_SIMILES` 独立常开
  （黑板/键盘/鼠标/摄像头/投影/狙击/雷达/像素/充电/电路…），`worldview.modern_similes`
  可覆盖，豁免仍走 `modern_words_exempt`；`_lexicon_check()` 输出
  「现代喻体「w」出戏（叙事修辞用了现代物）」。首版含"屏幕"，实测误伤
  前世指称（"电脑屏幕赶论文"）后抽离。测试 `test_m4_quality` +3。
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
- **大纲覆盖度检查**：~~docs/05 的「检查员」未实现；实测 22 建档人物中 3 人正文零出场且无告警~~ **已落地（R-CAST，提交 be3bc43 后）**：
  `consistency/rules.py::_cast_coverage_check`——建档人物在已写正文零出场 → warn；区分「计划出场（first_appear）已越过但跳票」（强信号）、
  「无 first_appear 无法判断」（弱信号）与「first_appear 在未来」（渐进写作正常，不告警）；单字名/已死亡退场人物跳过；无正文静默。
- ~~审计日志完整（F7.1）~~ **已落地（提交 16dfa0f 后）**：produce_chapter 整章 LLM 用量聚合（`_UsageCounter` 包装 provider，
  覆盖嵌套函数与编纂调用）→ 出口双落——`reports/stats/generation-<ts>.md`（人读持久，与 forge 报告同口径计价）
  + `.index.db` `audit_log`（机器查，ADR-016 辅助索引；修复原 ts 写死 "now" 占位）。成功/软 block/异常/落盘失败四路径都写。
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
| F0 | `state.py`（Blueprint/provenance）+ `slots.py`（槽位与缺口检测）+ Genre Pack 装载 + `forge show` | ✅ 完成（forge 包 + 2 类型包 + CLI show，见下） |
| F1 | 模式一全权：seed 提炼 + 授权询问 + 递归引擎最小树（book→volume→chapter，**卷闸门 vol=1**）+ 落盘（含 worldstate 确定性合成）+ provenance 保护 | ✅ 完成（forge/seed+nodes+engine + CLI seed/build/resume，AG1 通过，见下） |
| F2 | 商讨：分轮分组问答 + 候选批量生成 + transcript 续跑 + 非 TTY 降级 + 自由答案 | ✅ 完成（forge/ask+io_console + CLI resume 分流，见下） |
| F3 | 模式二 ingest：切章预览/抽取（超限降级确定性）/消歧/文风画像/卷章编码/记忆初始化/实体 warm-up + 缺口回落商讨 | ✅ 完成（forge/ingest.py + CLI forge ingest，见下） |
| F4 | 递归深化：worldview/character/style/threads 旁支节点 + 可选 arc/beat 层 + `forge roll` 滚动生成 + Genre Pack 扩充 | ✅ 完成（2026-09-01，见下） |
| F5 | `validate.py` V1–V6（含 FakeProvider 可写冒烟）+ report 双写 + rollback/--diff + pipeline 推进到「细纲」 | ✅ 完成（2026-09-01，见下） |

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

**F0 落地记录（2026-09-01，Forge 骨架）**：`src/novelist/forge/` 包建起——state / slots / genres /
2 个 Genre Pack 数据 + CLI `forge show`，全部确定性（零 LLM）：

- **schemas/forge/**（F0' 遗留项补齐）：`blueprint.schema.json`（蓝图中间态——rev/provenance/
  meta/worldview/characters/locations/items/skills/settings/threads/style/volumes/chapters/ingested，
  `role` 为蓝图内部字段、characters 用 `characters[role:protagonist].name` 伪路径兼容 provenance
  的 id 索引键；meta 补 `endgame`/`time_origin`）+ `genres.schema.json`（类型包自身可校验）；
  `project.schema.json` 加 `forge` 段（mode/interaction/stage/blueprint_rev/calls_used）。
- **state.py**：`Blueprint`（blank/load/save 均过自身 schema 校验、点路径 get/set 含数组下标、
  upsert 按 id 幂等、`set_provenance`/`is_protected`（src=user 永不被覆盖）/`low_confidence_paths`
  /`provenance_summary`）；`ForgeState`（project.json.forge 段读写，project.json 缺失时容错建骨架）；
  `append_transcript`/`read_transcript`（transcript.jsonl 留痕，F2 续跑前置）。
- **slots.py**：Slot 数据类 + 必填 8 槽 + 推荐 9 槽（docs/10 §5.3 表）；`detect_gaps`（未填 /
  provenance 低置信两类缺口，required 优先排序，`characters[role:*]` 按角色匹配无下标依赖）；
  `group_slots`（4 轮 × ≤4 问）；`slots_for_genre`（Genre Pack.slots 覆盖候选/默认值）。
- **genres/**：修仙男频（levels/词表/character_slots/节奏/默认文风/禁用词/卷弧提示）+ 通用
  （unavailable_states 基础词表，其余空——可写任何类型）。`genres.py` 装载：id 精确 → 别名匹配
  → 通用兜底，装载即过自身 schema 校验。
- **CLI**：`novelist forge show <dir>`（项目直传或根唯一项目）——打印蓝图 rev/阶段/调用数/
  meta/规模/人物伏笔卷章计数/缺口清单/provenance 分布。F1 的 seed 与 F2 的商讨命令挂同一 group。
- 测试：`tests/test_m12_forge_f0.py` 21 用例（schema 自身校验 / 装载与兜底 / 缺口检测确定性 /
  provenance 保护 / upsert 幂等 / ForgeState 往返 / transcript / CLI 集成）。全量 **314 passed**。
- 注意：`Blueprint.filled()` 不认识 `characters[role:*]` 伪路径（缺口检测 `_resolve_key` 才解析）——
  引擎填充时应走 `bp.section("characters")` + role 过滤，勿直接 `bp.filled()`。

**F1 落地记录（2026-09-01，模式一全权构建）**：`forge/seed.py` + `forge/nodes.py` + `forge/engine.py`，
AG1（一句话 + FakeProvider → `chapter 1 1` 可直出且 `bible_injected=True`、cast 非空）端到端通过：

- **seed.py（模式一入口）**：种子提炼（1 次 LLM）→ 蓝图初始化 → 授权询问 → 全权构建。
  `SeedSpec`（genre/template_suggestion/logline/protagonist_hint/conflict/tone_hint/scale_hint/
  time_origin/unknowns）；`_parse_seed_spec` 宽松解析（剥 ```json 围栏 / prose wrapper）；
  解析失败走确定性兜底（`_fallback_spec`，仍可构建）。`--smoke` 只提炼建蓝图不构建。
  interactive 非 TTY 自动降级 auto（写 `seed.downgrade` transcript）。
- **蓝图初始化 provenance 分层**：CLI 显式参数→`user`（受保护，模型永不可覆盖）> 提炼→`llm` >
  包默认→`template`；主角骨架建档（`char:{slug}`）。
- **engine.py（递归最小树）**：DFS 确定性展开 book→volume→chapter，**卷闸门只展开 vol=1 的 chapter**
  （B2 拍板）；硬边界 max_calls=60 / max_depth=4 / max_width=4 / 每节点 retry=1，失败回退父层产物，
  预算耗尽停止并标红。`resume` 幂等续跑：已落盘节点跳过、calls_used 不增长；重跑覆盖写（人物/伏笔
  按 id upsert）。节点协议 `{"artifact", "decide": done|expand, "reason", "children", "open_questions"}`
  （非法 decide 兜底 done）。
- **nodes.py（三节点 prompt + apply）**：`_book_prompt` 骨架+卷主线一次出齐；`_volume_prompt` →
  `outline/volumes.json`（upsert by vol，chapter_range 确定性计算）；`_chapter_prompt` 含前一章因果
  连续/after_days/directives → 细纲 md（悬空角色引用丢弃）。`_apply_book` 合并 worldview/characters/
  threads/style/volumes 时逐键过 provenance 保护（power_system 二级路径也逐键检查）。
- **细纲双通道**：front-matter JSON（`parse_gist` 消费）+ 行内 `key_events:`/`出场人物:`
  （`parse_key_events`/`parse_cast_decl` 消费，JSON 引号格式兼容 regex 解析）。
- **sync_bible**：蓝图→bible 全量重写（剥 role→is_protagonist、补 status/id/默认值、style 补
  protagonist 引用）；`synthesize_worldstate` 确定性合成（time={now:0, origin_text}、characters
  初始态、pending 空——零 LLM）。
- **CLI**：`forge seed`（--provider/--smoke/--volumes/--chapters-per-volume/--max-calls）、
  `forge build`（--provider/--force）、`forge resume`。fake provider 演示走固定 SeedSpec。
- 测试：`tests/test_m13_forge_f1.py` 18 用例（提炼解析/兜底/非 TTY 降级/节点协议/双通道/apply 保护/
  sync_bible 剥离/worldstate 确定性/AG1 端到端/resume 幂等/预算耗尽/解析失败回退/CLI smoke）。
  全量 **332 passed**。CLI 冒烟（fake）：`init + forge seed --volumes 2 --chapters-per-volume 3` →
  2 卷主线 + 3 细纲 + bible 5 文件 + worldstate，`forge show` 阶段=built、调用=6。
- 真实链路修复：LM-Studio 冒烟暴露 `response_format.type=json_object` 400（兼容层只认
  json_schema/text）。修复 `providers/lmstudio.py`：json_object 请求剥掉 response_format，
  靠 prompt 引导 JSON（forge/抽取层解析器本就宽松兜底）。

**F2 落地记录（2026-09-01，商讨问答协议）**：`forge/ask.py` + `forge/io_console.py`，
docs/10 §5.3 协议落地——问题由引擎（确定性）决定、候选由模型（LLM）批量生成：

- **io_console.py**：`AnswerIO` Protocol（is_tty/notify/ask_choice/ask_free/confirm）+ `ConsoleIO`
  （可注入 `_in`/`_out`；回车=推荐值、`N xxx`=第 N 项自由答案、`N`=选第 N 候选、`q`=退出）。
- **ask.py**：槽位缺口检测（unfilled/low_confidence）→ `group_slots` 分轮（3–4 轮 × ≤4 问）；
  每轮 1 次 LLM 批量生成候选（`_gen_candidates`，失败回退 `{}` 记 `candidates.fallback`，不阻断商讨）；
  `parse_round_line` 纯函数解析用户行（乱码保守全默认）；`_apply_slot_value` 按槽位类型写蓝图——
  `characters[role:*]` 直接改 section 对象（伪路径不走 `bp.set`）、threads→`pt:{slug}` upsert、
  style.glossary 按 term 查重、list 类拆 `、`/空格、空值/占位值保持缺口。
- **transcript 续跑**：`ask.answer`/`ask.skip` 事件逐项落 `transcript.jsonl`；`_answered_keys` 以
  已答 key 集合判据，resume 幂等续问（跳过已答槽位）。
- **seed 集成**：`_authorize_ask` 返回 `"auto"|"consult"`（去掉"[2] 尚未实现"占位）；consult 分支
  `touch_stage("consulting")` → run_consult（slots_for_genre(pack)）→ 无 quit 则 `touch_stage("seeded")`；
  quit_early 保持 consulting、不构建（`SeedResult.quit_early=True`）。
- **CLI**：`forge resume` 新增 `--provider`；按 `state.stage` 分流——`consulting`→run_consult 续商讨，
  否则→build 续跑。`forge seed` quit_early 时提示"resume 继续商讨 / build 直接构建"。
- 测试：`tests/test_m14_forge_f2.py` 17 用例（parse_round_line 5 / ConsoleIO 原语 / run_consult 7：
  非 TTY 降级+AG3 收敛 answered≤12、TTY 自由答案 src=user conf=1.0、每轮恰好 1 次 LLM 调用、
  候选失败回退、结构化写入 threads/glossary/rival、resume 幂等、q 提前退出 / seed 集成 2：
  quit_early 不构建、商讨完整→继续构建 / CLI 2：resume 按 stage 分流）。
  全量 **350 passed**（2 deselected 慢测试）。

**F3 落地记录（2026-09-01，模式二 ingest）**：`forge/ingest.py` + CLI `forge ingest`，
docs/10 §6 流程落地——已有稿子 → 蓝图 + 正式章节 + 接着写：

- **摄入与切片**（§6.1）：`slice_chapters` 目录/文件列表（--recursive，.md/.txt，空文件跳过）→
  `第X章`/`Chapter N` 标题行优先（**容忍数字与章节字间的空格**：`第 1 章` 也认），识别不到按
  空行段落 + 目标字数兜底切；`preview_chapters` 预览确认（回车=全部确认 / `R`=全按字数重切 /
  `N`=从第 N 章起重切 / `q`=退出不写库；非 TTY 自动直过）。
- **抽取**（§6.2）：`extract_deterministic` 纯正则零 LLM——2/3 字名启发式（**同族取频次高者为主名**，
  叶蓝心 ≥ 叶蓝 保留全名、陆沉X < 陆沉 归并；姓氏表补常用缺漏姓叶/龙/齐…；`_ORG_STOP` 滤组织通名
  宗门/弟子…）、称谓共现提权、专名 `_match_suffix`（**动词/虚词前缀截断**：服用洗髓丹→洗髓丹）、
  pending 双语序（闭关三月后 | 三日后闭关，`[^。！？\n]` 防跨句）、对白占比/句长指标。
  LLM 补语义（性别/境界/性格/别名/关系/key_events/pending/伏笔/文风），**独立配额
  `--ingest-max-calls`**，超限/失败/无 provider 全部降级纯确定性 + `downgraded` 章号 + warnings 披露
  （docs/10 §12 不静默）。
- **归并消歧**（§6.3）：`merge_characters` 别名归一（叶岚/叶师弟 → 同一 `char:`）、频次 ≥3 进主线、
  LLM relation=主角 或最高频者 → protagonist；**id 序号制**（`char:in1`…）——中文名不可作 id
  （schema 限 `^char:[A-Za-z0-9_-]+$`），`_slug` 会全消中文导致 id 撞车互相覆盖（修复点）。
- **文风画像**（§6.4）：`style_from_ingest` pov/target_words 取包默认，tone 由 style_notes 归纳，
  provenance=ingested（后续商讨只 confirm 不重问）。
- **卷章编码 + 记忆初始化**（§6.5）：N 章按 chapters_per_volume 编入卷 → `chapters/<vol>-<ch>.md`
  （**已是正式章节**）+ 细纲反写 `done=true`；chronicler 逐章抽事件写 `memory/` + `MemoryIndex.rebuild`
  （第 N+1 章"先忆"非空）；`EntityTracker.update_from_chapter` 逐章 warm-up 重建
  `bible/entity_progress.json`；worldstate 确定性合成 + pending 结构化（`parse_pending_line` →
  `{"id": "pd:ingestN", "what", "due", "status"}`）。
- **缺口回落**（§6.6）：interactive 且 TTY 才回落 run_consult（slots_for_genre(pack)）；无 provider
  跳过并告警；stage 流转 ingest→ingested。
- **CLI**：`forge ingest source [directory]`（--provider 缺省 fake=纯确定性链路 / --genre-pack /
  --chapters-per-volume / --target-words / --ingest-max-calls / --mode / --dry-run / --recursive），
  成功提示"下一步：`chapter {vol} 1` 接着写（N+1 章）"。
- 测试：`tests/test_m15_forge_f3.py` 26 用例（切片 5 / 预览 5 / 确定性抽取 4 / 消歧 2 / 降级与披露 3 /
  全链路 1 + dry_run 1 + interactive 回落 1 / 记忆初始化 2 / CLI 2），全量 **376 passed**
  （2 deselected 慢测试）。功能冒烟 `novel_workspace/_harness/f3_smoke.py`（FakeProvider 全链路）。

**T4 落地记录（2026-09-01，Forge 联动，随 F3 实施）**：

- **细纲 after_days → pending 登记**（M3m T4 主件）：`nodes.synthesize_worldstate` 遍历蓝图
  `chapters` 按 (vol, ch) 排序累计 `after_days`（相对天数轴，now 从 0 起），>0 的章登记
  pending（due=累计值，id `pd:ke-<vol>-<ch>` 与生成期数字 id `pd:N` 不冲突，what 取首个
  key_event、空则回落章标题，字段与 `timeline.add_pending` 全对齐：span/created_t/status=
  scheduled/overdue/block_count），渐进提醒分档开箱可用。build（engine finally）与 ingest
  两条链路共用此合成，零 LLM。
- **ingest 约定条目对齐**（F3 遗留 bug 修复）：ingest 抽取的「约定：」pending 曾写
  `status: "pending"`——不在 worldstate schema 枚举（scheduled|fired|cancelled|expired），
  且 timeline 下游 tick/软 block 只认 scheduled，导致 ingest 登记的定时事件**永远不会被
  提醒/拦截**；改为 scheduled + 补齐 span/created_t/created_at/overdue/block_count。
- **ingest 时间轴保留**（用户拍板「预计完成时间」语义，2026-09-01）：ingest 末尾曾用
  `synthesize_worldstate(now=0)` 无条件覆盖 worldstate，把 chronicler 逐章推进的
  time（「时间：」行 → advance）与按各章 day 锚定的 pending（「约定：」行 → add_pending）
  全部抹掉。改为：**不覆盖时间轴**，仅把蓝图合成的人物初始态合并进去（保留 chronicler
  打的 unavailable_*/history）；确定性正则抽「约定」降为无 LLM 兜底（due = 当前 day + dt）。
  pending 统一语义：**全部条目 = 预计在 day:n 前后发生/完成的事**（细纲 after_days 条目
  从生成期视角即未来日程，与编纂员「约定：」行同一语义池）。
- 测试：`tests/test_m16_forge_t4.py` 6 用例（after_days 累计登记 / what 回落标题 /
  无 after_days 兼容 ingest / 乱序章排序累计 / **jsonschema 校验 worldstate** /
  ingest 行格式对齐），另 test_m15_forge_f3.py 增时间轴保留用例（now=60 不被覆盖、约定按各章 day 锚定），全量 **383 passed**。

**F4 落地记录（2026-09-01，递归深化 + arc/beat + forge roll）**：

- **旁支递归 DFS**（`engine.build(deepen=True)`，次序 worldview → character_group → style →
  thread_set，自定决策：设定→人物→文风→伏笔，伏笔最后可引用前面产出）：每节点走 §7.2 协议
  （decide/reason/children + 硬边界），worldview expand → system（settings 条目落库）→
  setting_entry；character_group expand → character（骨架→完整卡，relationships 交叉引用
  校验由 prompt 纪律约束）；style/thread_set 为叶。产物经蓝图 provenance 保护落 bible。
- **节点增量落盘**（docs/10 §7.4 补齐）：每节点写 `nodes/<node_id>.json`
  （kind/decide/reason/artifact）；node_id 含 `:` 在 Windows 文件名非法 → 净化为 `-`
  （自定决策，文档未提）。resume 双判据：chapter/volume 用产物存在性，旁支/arc/beat 用 nodes/。
- **arc/beat 层**（自定决策落盘位置，docs/10 §7.5 表原缺 arc/beat 行，已回填）：
  volume expand → arc 节点 → `outline/arcs.json`（独立文件；blueprint schema
  additionalProperties:false 不允许 arcs 段，arcs 不进蓝图）；chapter expand → beat 节点 →
  gist.beats 并入细纲 front-matter（beat 属章，不单独建文件）。chapter prompt 注入本卷
  arc（K 章均匀分配，确定性）。
- **`forge roll <vol>`**（`engine.roll` + CLI）：前置 vol≥2 且前卷有正文；§7.7 四块注入
  （前卷主线+伏笔兑现 / 前卷末 3 章实际发生 plot_events / worldstate 现状 / payoff_checklist）；
  volume(expand→arc) → chapter×K（prev 链从前卷末章接起）→ beat?；预算 40/卷（§7.3）。
  末尾幂等追加新卷 after_days → worldstate.pending（**不覆盖 time/characters**——ingest
  时间轴修复同款纪律；due = 当前 now + 累计）。build/roll 前置文件快照推迟 F5（rollback 属
  F5 范围，本版依赖版本轨 + nodes/ 增量落盘）。
- **deepen 开关**：build/run_seed 默认 True；`--no-deepen`（build/resume）退化 F1 最小树。
  存量 F1 行为测试显式 deepen=False。resume 顺手修复 F1 隐患：volume 已落盘时原 `continue`
  会整卷跳过章循环，改为落入章循环逐章幂等检查。
- 测试：`tests/test_m17_forge_f4.py` 8 用例（旁支 DFS 落库+计数 / nodes 落盘+resume 幂等 /
  arc+beat 层 / max_width 截断告警 / roll 四块注入+pending 追加 / roll 幂等 / roll 前置拒绝 /
  CLI roll），全量 **391 passed**（2 deselected）。

**F5 落地记录（2026-09-01，契约校验 V1–V6 + 报告双写 + 快照/rollback + --diff + pipeline 推进细纲）**：

- **`validate.py` V1–V6**（docs/10 §9 全量落地）：V1 双层 schema（`BIBLE_CONTRACT` 14 类映射 +
  jsonschema 逐条）、V2 交叉引用（坏 char 引用 / 卷章区间缝隙 block）、V3 覆盖度（主角卷 1 第 1 章
  出场 / settings 下限可配 `--settings-min` / 卷 1 threads_to_payoff 非空）、V4 可写冒烟
  （`--smoke`：FakeProvider 跑 `produce_chapter(1,1)` 断言 ok/bible_injected/cast——**单测绝不真调
  LLM 纪律的延申**，docs/09 §2.1）、V5/V6 质量软检查（warn 不阻断，写入 report.md）。
  **自定决策：仅卷 1 缺失细纲报 block**——build 只产出卷 1，卷 2+ 由 `forge roll` 渐进生成，
  未 roll 不算阻断（最初全卷报 block 会误伤合法渐进流程）。
- **报告双写**（`report.py`）：全量 → `workspace/forge/report.md`（resume/build 引用），摘要 →
  `reports/stats/forge-<时间戳>.md`（解决 reports/ 零写入遗留）。含 provenance 来源分布表 /
  V1–V6 校验结论 / 调用与耗时 / token 与估算成本（¥1/1M in + ¥2/1M out，DeepSeek 参考价）/
  每节点 decide+reason 摘要。数据全确定性零 LLM。
- **usage 上链**（自定决策，docs/10 §4.3 已拍板）：`NodeResult` 补 `tokens_in/tokens_out`，
  `engine._call` 记 transcript `build_node` 事件（含 retried 标记），report 据此聚合成本。
- **双快照机制**（自定决策，docs/10 §7.6 补充）：build/roll **前置**快照（回滚前提）+ **结果**
  快照 `build-ok`/`roll-ok`（`--diff` 比对基线与 `forge rollback` 默认回滚点——最初 diff 基线取
  前置快照（seed 态）与构建结果天然全差、误报全量重建，补结果快照修复）。快照目录
  `workspace/forge/snapshots/<ts>-<label>/`，纳入 project.json/bible/outline/blueprint.json/nodes；
  transcript 与快照自身不入快照。
- **`--diff` 影响分析**（docs/10 §7.6 落地）：比对当前蓝图 vs 最近结果快照 → 结构级
  （worldview/style/volumes/arcs）变更 → 全量重建；实体级（characters/threads/settings）变更 →
  按章引用精确匹配受影响章，settings 只清 setting_entry 节点。`forge build --diff` 先清理产物
  再提示全量重建。
- **CLI 新增**：`forge validate <dir> [--smoke] [--settings-min N] [--no-report]`（通过则
  `Checkpoint` 持久化 `pipeline_state=细纲`，只前进不倒退；block 不推进）、`forge rollback
  [--to <name>]`（缺省最近快照 = build-ok 结果态）、`forge snapshots`（列快照目录）。
- **`ForgeState.save` 同步 checksum**（存量缺陷修复）：Forge 每次写 project.json 但从不刷新
  `.checksum.json`，validate 首次在 build 后调 `Checkpoint.restore` 即暴露「file checksum
  changed」误报 → save 末尾同步刷新。
- **schema 对齐（自定决策）**：`project.schema.json` 的 forge.stage 枚举扩展为实然值
  （seeded/built/consulting/ingested 等，存量枚举停在 F0 初版导致 `forge.stage='built'` 违约）。
- **白名单过滤修订（本轮修正）**：`sync_bible` 按 schema 白名单过滤（factions 归一化 +
  plot_threads/settings/items/locations/skills 对齐 `additionalProperties=false`），但 thread_set
  节点协议要求产出的 `plant_desc/payoff_desc` 最初被一并过滤——**F4 测试证伪**（丢伏笔语义，
  模型白写），改为 `schemas/bible/plot_threads.schema.json` 收录两字段 + 白名单恢复。
- 测试：`tests/test_m18_forge_f5.py` 15 用例（合法全过 / V2 block×2 / V3 block×2 / V4 冒烟 /
  V5 V6 warn 不阻断 / 报告双写+usage / CLI 推进与不推进 / 双快照+rollback / no-snapshot 抛错 /
  diff 精确与全量重建 / CLI snapshots+rollback），全量 **406 passed**（2 deselected）。

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
| T4 | Forge 联动：细纲 `key_events` 可选 `after_days` 登记 pending + ingest 抽取"三个月后"类约定 + build 末尾 worldstate 确定性合成（`time={now:0, origin_text}`，人物初始状态从蓝图合成，零 LLM）（随 M3l F3/F4 实施） | ✅ 完成（随 M3l F3 落地，见下） |

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

**3080ti 实测定时事件事故与修复（2026-09-01，v6 ch1-10 批量）**：

- **事故**：ch7-10 全部被 R-TIME 软 block 拦截（「叶蓝出关」逾期 5-6 章）。排查发现
  `worldstate.pending` 堆积 **11 条**同文「叶蓝出关」（due 90/180/270…依次 +90）——
  根因是**编纂提示词 few-shot 示例硬编码旧主角名「叶蓝」**（bible 早已改名「叶岚」），
  模型照示例形状每事件编纂凭空输出「约定：叶蓝出关｜+90日」；resolve_who 匹配不到
  char:yelan → who 空 → 与正名「叶岚出关」（已 fired）无法合并，每章 2-3 条持续堆积。
  R-TIME 机制本身**按设计工作**（拦截了垃圾数据），但整章硬失败浪费 4 章额度。
- **修复（三层）**：
  1. **提示词根因**：EXTRACT_PROMPT few-shot 示例 叶蓝→叶岚 + 强化"正文没出现的承诺不要写"
     （模型发明未来承诺是事故的原料）。
  2. **同章去重**：`_apply_pending` 按"剥离姓名前缀的动作核"（含 **1 字编辑距离近形名**归并，
     如 叶蓝/叶岚）对**本章已登记**的同核约定跳过登记——跨章保留（ADR-019 绝对锚定语义，
     test_ingest_preserves_chronicler_timeline 的 due=3/33 各自登记不可破坏）。
  3. **数据清理**：proj-yelan2 worldstate 移除 11 条幽灵 pending。
- **测试**：test_m10_timeline +2（同章近形名去重 / 提醒注入改为真验证——原断言命中
  EXTRACT_PROMPT 示例文本属假阳性，改为断言轻提示档「叶蓝出关（还有 X 天）」真实注入
  + due 调 to now+5 确定性触发）。
- **教训**：① 提示词示例里的具体人名是"活数据"，改名后必须全仓 grep；② 测试断言要
  指针对机制而非凑巧命中的文本；③ 软 block 拦截生效即说明数据源出了问题——先查数据
  再怀疑机制。

验收：闭关事件登记 pending 且三个档位（30%/10%/到期）依次触发对应提示；闭关人物在不可出场期
出现在正文被 R-STATE 告警；`timeline.json` 非空且 R-TL 按 t 单调；全量测试不回归（262 passed）。

### M3n — 生成期人物一致性四件套（ADR-020，2026-09-01 拍板 / 2026-09-02 落地）

> 设计：`docs/03` ADR-020。用户四项拍板：①延迟拟标题；②人物卡**无条件全字段**注入；
> ③人物调度层（character direction sheet）；④角色视角记忆（perspective memory）——
> B 方案改为**事件末调用**，一次总结输出全部出场角色的**多视角差异**；
> 回读采用**双阈值**（事件 gap ≥ 3 **或** 天数 gap ≥ 30，OR）。用户另提「世界广播选角」
> （事件选人阶段 +1 次调用广播事件信息、由 AI 决定人选）列为后续 ADR，本批不实现。

**为什么单列**：前几批把 bible/记忆/检索做扎实，但正文生成仍是"裸写"——人物卡不进上下文
（P0-1）、草稿无人物调度、视角记忆空白，成稿实测出现性别漂移/凭空造人/卷名漂移。本批四件事
一次接通，每事件 +2 次 LLM 调用（调度 + 视角总结）、每章 +1 次（拟题）换取人物一致性
（用户明示可接受更多调用换质量）。

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| N1 | **延迟拟标题**（defer_title）：`render_gist_md` 行内不再写标题（仅 `# 第 {ch} 章`，title 留 front-matter）+ 事件 prompt 禁标题 + 章末一次 `_title_chapter`（temp 0.3 / ≤64 tokens）→ `_apply_chapter_title` 写回正文首行与 outline front-matter | ✅ |
| N2 | **无条件人物卡注入**（cast_injection）：`director.match_cast`（姓名/别名/子串三回退）+ `cast_from_text` 扫文本兜底；注入字段 6→9（补 arc/aliases/relationships） | ✅ |
| N3 | **人物调度层**（character_direction）：事件生成前 +1 次 LLM 调用产出各角色「性格要点/本场体现/禁忌」direction sheet，随事件注入 | ✅ |
| N4 | **角色视角记忆**（perspective_memory）：**事件末** +1 次调用一次输出全部出场角色的视角条目（stance/perspective/relations + event_ref），`chronicler.record_perspectives` 落 character_histories，事件去重幂等 | ✅ |
| N5 | **双阈值回读**（needs_readback）：角色距上次出场 **≥3 事件 或 ≥30 天**（OR）即注入近况行 + `readback_excerpt` 回读上次出场正文片段（1200 字 × 2 段） | ✅ |
| N6 | 兼容与回归：5 个存量脚本化 `_SeqLLM` 测试显式关闭四开关；`tests/test_m19_adr020.py` 24 例全假 provider 覆盖四件套 + P0 回归 | ✅ |

**N1–N5 落地记录（2026-09-02）**：

- `forge/nodes.py render_gist_md`：行内标题文字去掉，title 保留 front-matter——正文生成器不再
  预先见到标题，杜绝卷名漂移/标题剧透；`parse_gist` 仍可读 front-matter title。
- `core/director.py`（新模块）：调度层与视角装配/回读全量落此。`build_direction`/`_parse_directions`
  产出调度表；`match_cast`（姓名/别名/子串回退）+ `cast_from_text` 兜底；`render_card_line` 渲染
  9 字段卡片；`render_history_lines`/`recent_perspectives` 供近况注入；`needs_readback` 双阈值；
  `readback_excerpt` 回读上次出场正文；`save_direction` 存 `memory/directions/v{n}-c{n}-e{n}.json`。
- `core/orchestrator.py`：事件循环接线（cast → 近况 → 回读 → direction_lines → 生成 → 视角回写）；
  四开关默认 True；章末 `_title_chapter` + `_apply_chapter_title` 拟题写回。
- `core/chronicler.py`：`record_perspectives`（自 455 行，事件末多角色一次调用，kind=perspective，
  event_ref=`ev:{pid}:{vol}:{ch}:e{index}`），MemoryConflictError 去重返回 0（幂等）。

**本批修复的真实 bug**：

- **`cast_from_text` 短名子串误命中**：短名命中已命中长名的子串时重复添加（「清瑶」⊂「云清瑶」
  被当两人）→ `hit_names` 拦截 `any(nm in hit)`。
- **`_TITLE_INLINE_RE` 贪婪吞正文**：`[^\n]{0,40}` 把标题后同行正文吞成标题 → 收紧为「非标点/空白
  紧密字符」`[^，。！？；：、\s\u3000]{0,30}`。
- **chronicler.py:515 缩进损坏**（前次会话 429 中断的半成品）→ `for item in re.split(...)` 循环体
  修复，py_compile 通过。
- **续写拼接无痕化**：`_generate_with_continuation` 内续写可能断在**句中**，插 `\n\n` 产生割裂 →
  恢复 `text.rstrip() + strip_seam_overlap(text, piece).lstrip()` 无缝拼接；事件**间**段落分隔
  仍由 `"\n\n".join(pieces)` 负责（两处职责不同）。
- **polish 透传**：`polish_chapter` 未把 system_prompt 传到底层（P0 修复）；
  `completeness()` 跳过起始标题行 + 新增 dup_paragraphs/dup_sentences 重复检测。

验收：produce_chapter 每事件产出 direction sheet 与全部出场角色视角条目（含 stance/relations，
冲突去重幂等）；角色距上次出场 ≥3 事件或 ≥30 天时正文前注入近况与回读片段；章末拟题写回文件头
与 front-matter；M19 24 例 + 存量全量 **438 passed（2 deselected）**；5 个旧脚本化测试显式关
开关不回归。

### M3o — Markdown ⇄ Word(.docx) 互转（2026-09-02 用户新增需求）✅ 已完成

**需求**：系统两端都要能接 Word——① 已有 Word 稿转 md 喂进系统接着写；② 成稿输出 Word 交付。
用户明确范围：**只做互转本身**（不改造 ingest 切片逻辑与 export 发布包结构）。

**选型：零第三方依赖**。项目依赖刻意精简（pyproject 只有 click/pydantic/fastapi/
jsonschema/structlog），python-docx 未安装且会引入 lxml 传递依赖。docx 本质是
zip + OOXML，用 stdlib `zipfile` + `xml.etree` 直读直写即可，产出 `core/docxconv.py`。

| 能力 | 实现 | 关键点 |
| --- | --- | --- |
| docx → md | `docx_to_markdown()` | 标题靠 `w:outlineLvl` / 样式名（Heading N / 标题 N）三级判定；粗斜体/链接/表格/列表（按 numbering.xml 判 bullet vs decimal）还原；图片抽到 `<stem>_media/` 并写相对引用 |
| md → docx | `markdown_to_docx()` | 首个 H1 升格书名页 + 每个 H1 段前分页；正文宋体小四 / 1.5 倍行距 / 两端对齐 / **首行缩进两字符**；表格用原生 `w:tbl`；链接生成真 `w:hyperlink` + 外部关系 |
| CLI | `novelist docx to-md` / `to-docx` | 输出路径缺省同目录同名换后缀；字体/缩进/分页可配 |

**四个必须记住的坑**（都写了回归测试）：

1. **格式下沉到样式，run 级不放**。标题加粗、引用斜体若写在 `w:rPr/w:b` 上，
   docx→md 读回会被还原成 `**标题**`，往返不干净——且转出的 md 要喂回 ingest，
   `**` 会污染语料。正解是写进 `styles.xml` 的 Heading/Quote 样式，run 只留文本。
2. **行内代码靠字符样式往返**。`CodeChar` 字符样式打标记，读侧识别后重加反引号；
   否则 mono 字体在 docx 里无从还原。
3. **XML 非法控制字符必须剔除**（`_xml_safe`）。模型偶发的 0x0B/0x0C 会让 Word
   直接报"文件损坏"，这是转换类代码最常见的翻车点。
4. **相邻 `w:tbl` 之间要插空段落**，否则 Word 判定包损坏。
5. **OOXML 子元素顺序敏感**（自查发现，测试抓不到）：`w:pPr`/`w:rPr`/`w:tblPr` 的子元素
   必须严格按 CT_PPr 序列排（如 rStyle→rFonts→b→i→strike→u→color→sz；pPr 中 shd 在
   spacing 前、spacing 在 ind/jc 前；tblBorders 在 tblLayout 前；outlineLvl 最后）。
   `ElementTree` 解析不校验顺序、pytest 全绿，只有 Word 打开才报"文件已损坏"。
   已修 5 处（13cf594）。

**系统接线（1250fd1，用户后续要求补全两端）**：
- **输入**：`forge ingest book.docx` 直达——`_gather_files` 收 .docx，
  `_convert_docx_files` 预转同名 md（图片不导出；同名 md 视为派生物，重跑以 docx 为准覆盖）；
  `_CHAPTER_RE` 兼容 `# 第X章` 标题前缀（docx 转出的 md 标题带 `#`），章题剥 `#` 入细纲。
- **输出**：`export --format docx --output book.docx` 直接产 Word 成稿
  （`export_project` 出 md → `markdown_to_docx`；`--output` 必填，无法打印二进制）。
- 测试 +5（切片转换/带#标题/docx 全链路 ingest/export docx 往返与缺参）；
  真机冒烟：天道修改器.docx dry-run 切片正常、yelan3 草稿 16791 字导出 docx 读回保真。

**验收**：22 条新测试（`tests/test_m20_docxconv.py`，含手工构造极简 docx 精确测读侧）；
真机跑 yelan3 五章 16695 字往返——去空白后 15681 字符**零丢失**，5 个章标题 +
书名页结构保真、关键实体无缺失。全量 **473 passed, 0 failed**（接线后）。

**与既有流程的接缝**（未改动，仅路径指引）：转出的 md 直接喂
`novelist forge ingest <目录>`（`_gather_files` 收 .md/.txt，天然兼容）；
成稿 `novelist export --output book.md` 后再 `docx to-docx`。

### M3p — P0 闸门落地（2026-09-02 用户拍板开工；✅ P0-A/B/C/F 全部完成，P0-A 数据补喂见 M3r）

依据 `prompt作用审计.md` 优先级表：修复重心不是改 prompt 文案，而是
**补数据（P0-A）+ 加确定性闸门（P0-B/C）+ 删死代码（P1-F）**。
本轮先落代码侧（可测试闭环），数据侧 P0-A 随后单独做。

| 项 | 实现 | 提交 |
| --- | --- | --- |
| **P1-F** lessons 死参数 | `build_system_prompt`/`build_chapter_context` 删 lessons 形参（第 8 轮 RAG 化后函数体零引用）；review_lessons.json 仍走 knowledge.py 检索 | 0d382f0 |
| **P0-B** 设定追认闸门 | `_supplement_settings` 改纯提案器：新词一律进 `bible/settings_pending.json` 待人工确认，绝不自动入档；items/skills 名册纳入已知词（强化符钻的洞）；CLI `settings-pending --allow/--deny` 人工转正；指标 `settings_added`→`settings_pending`（含审计双落） | 0d382f0 |
| **P0-C** 编纂员入库闸门 | chronicler `commit` 三道确定性闸门：① 强成段地名未登记 → 事件拒收；② realm 值须过 `parse_realm`（worldview 境界表），体系外丢弃+warning；③ 近似去重（二元组 Jaccard ≥0.45 + 共同参与者）；报告新增 `rejected` 全程披露 | 4dbc703 |

**验收**：新测试 9 条（`tests/test_p0_gates.py`）+ 2 条旧测试语义更新
（m9 supplement 转待确认语义、m4 重复回退双路径）；全量 **482 passed, 0 failed**。

**关联决策**：世界广播/角色工厂联动用户拍板——广播=需求匹配器（池内找人优先），
工厂=缺货补货通道（生产→闸门→自动注册 active→本事件可入场），见 ADR-021/022
修订（2c5b74c）。P0-A bible 数据补喂与角色工厂实施已完成（见 M3q/M3r 条目）。

### M3q — 角色工厂实施（ADR-022，2026-09-02 用户拍板"3做完继续讨论" ✅ 已完成）

用户口径：广播先在可及池内找人 → 找不到合适人选 → 输出结构化缺人需求 → 转交工厂
生产 → 过确定性闸门 → 自动注册 active → 本事件即可注卡入场（同步）。JIT 补卡
（字面追认，与 P0-B 合围）**降级为告警器**——细纲声明出场但缺卡不再 LLM 补卡。

| 件 | 实现 | 提交 |
| --- | --- | --- |
| 需求队列 | `core/character_factory.py`：`CharacterNeed`（role/desc/realm_hint/faction_hint/hooks/source/vol/ch）+ `queue_need/load_queue/drain_queue`（bible/character_needs_pending.json，每章配额 ≤2 超出留队） | 27e5c5c |
| 生产管线 | `produce()`：LLM 草卡（最小卡规格约束）→ 五类确定性闸门（名字撞名册/境界 parse_realm ∈ 表/宗门 ∈ 名册/关系钩子 target 指向已存在角色且 ≥1/行为规格 2-3 条）→ 注册 active + provenance=factory | 27e5c5c |
| 事件循环接线 | `produce_chapter` 章前 `drain_queue` + `ProductionResult.factory_added`；`_jit_characters` 降级告警器（缺名入队 source=jit_alarm，队列幂等防重） | 27e5c5c |
| 文档 | ADR-022 状态改"已拍板/已实施"——配额 ≤2、触发源=广播+CLI、同步入戏、pending 确认 | d29ba2c |

**验收**：新测试 13 条（`tests/test_character_factory.py`，含 JIT 告警降级）；m9 旧 JIT
测试改新语义；全量 **495 passed**。修复 `_PROMPT.format` 传未定义 `hook_block` 的 NameError。

### M3r — P0-A bible 数据补喂（2026-09-02 用户拍板"1可以做"；✅ 全部完成）

注入面审计结论：`produce_chapter` 注入代码已齐（B-02 → `build_chapter_context` + ADR-020
`cast_injection` 事件级人物卡注入）——断点在**数据**：yelan3 实测 26 卡仅 3 张有
relationships、0 张有 behavior_rules，`render_cards` 注入的是无料卡，模型只能现编关系
→ 行为漂移/AI 味。修复 = 数据补喂（补数据是 prompt 的原料，同 P0-B 追认闸门哲学）。

| 件 | 实现 | 状态 |
| --- | --- | --- |
| 补喂工具 | `core/character_enrich.py`：缺料判定（rels/rules 空即缺）→ LLM 提案（输入=全员名册+该卡 character_histories+涉卡 plot_events，保证应然不与实然冲突）→ 确定性闸门（串卡/悬空 target/自指/重复/规则条数/age 越界）→ `characters_enrich_pending.json` | 代码完成，测试 8 条 ✅ |
| 确认通道 | CLI `characters-enrich --provider deepseek [--card 名]`（提案）+ `enrich-pending --allow/--deny`（合并入档：同 target 采纳提案描述/新 target 追加、rules 并集去重、age 只补缺、provenance=enrich；settings-pending 同款） | 代码完成 ✅ |
| DeepSeek 试点 | yelan3 三卡（李慕白/苏婉/秦叔）：提案质量好——关系与 arc/前情对齐，规则为带条件的可执行句，全过闸门入 pending | ✅ |
| 全量补喂 | yelan3 **26/26 缺料卡全部入 pending**（DeepSeek 提案，报告 `novel_workspace/proj-yelan3/reports/p0a_enrich_review.md`） | ✅ |
| 全量入档 | 2026-09-02 用户拍板：① 同 target 重复关系**采纳提案详细描述**（叶岚「寄生」→「寄生宿主，互相利用，系统来历成谜」等主角级简略句被覆盖）；② 26/26 全部 `--allow`。`enrich-pending` 全部入档：26/26 卡有 rels（1-3 条）+ rules（各 3 条）、15 卡补 age、全打 origin=enrich。pending 清空 | ✅ |
| 存量 schema 漂移修复 | 入档后跑 `novelist validate` 暴露：`memory/character_history` schema 缺 ADR-020 N4 视角字段（kind/stance/perspective/relations/event_ref）+ `at.t`（ADR-019）→ schema 补齐，proj-yelan3 契约校验全过（14 类映射） | ✅ |
| 注入面收口（零 LLM 复验） | 复跑前审计发现渲染缺口：`render_card_line` 渲染 9 字段**不含 behavior_rules**——补喂的 3 条可执行规则到不了模型。修复：① 渲染追加「行为：」段（rules ≤3 条整句，M3r 补喂字段进注入面）；② 关系目标 `char:xxx` 经 `render_cards(cards, all_chars=bible_chars)` 回查成**角色名**（此前显示半英文 id "yun"，模型认"云清瑶"不认"yun"）；③ gender=unknown（forge 占位）不再进 prompt。测试 36 条过，真卡复验：主角行含完整关系+行为 | ✅ 836877e 后追加（本提交） |

**验收**：人工审阅提案无与已发生事件冲突、确认入档后跑一章对照（关系/行为规则实际出现在
生成上下文）→ 目标：行为漂移/AI 味显著下降。**待办**：注入面收口已完成（渲染确定性复验
通过）；剩**真实模型跑一章**（章节 ≥6，v7 已写到 ch5）对照 v7 同章行为漂移——需消耗 API。

**✅ 验收完成（2026-09-02）**：DeepSeek 续跑 ch6（`_harness/run_ch6_deepseek.py` +
`ch6_deepseek_对照报告.md`，提交 15bccc8）。注入面吃到补喂料证据充分：叶岚「主动示弱/转移
话题/不暴露系统」行为规则全程在线（1-6.md 多处示弱三连）、系统被墨无极说破「五五开」时
反常沉默应激 = 补喂「寄生宿主，暗藏旧账」关系的行为化——对比 v7 主角侧人设崩坏零漂移。
6610 字完整收束，ADR-020 四件套/事件回写全链路正常。章末审校 14 条人工复核 8 条误报；
真问题 2 个（结尾钩子 L79/L237 重复、场间引用未发生对话）。混杂变量声明：模型与数据同变，
严格 A/B 待下次 qwen3.6 跑批同开补喂数据再量化。存量缺口再确认：bible set:wuwu 无绑定时长
条款（P0-A 已知待补项，正文被迫自创"短暂绑定"语义）。

### M3s — ADR-021 世界广播选角落地（2026-09-02；✅ 代码+测试完成，v1 默认关待真机验证）

M3r 验收后用户口径"做完继续讨论"的下一里程碑（人物一致性栈表层第 0 层）。把事件选角从
"细纲声明 + 文本字面兜底"的确定性集合升级为：事件级 +1 次 LLM 语义推理"谁该在场" →
确定性校验（五条硬约束）→ 名单驱动 `match_cast` 注卡。调度/视角的上游人选先定对。

| 件 | 实现 | 状态 |
| --- | --- | --- |
| 广播模块 | `core/broadcast.py`：可及池（worldstate dead / unavailable_until>now / bible status dead·unknown 剔除，零调用）；prompt（事件+接缝+天数+细纲声明+上事件+池≤40）；解析纪律（防造名拒绝+告警、```json 围栏容错、非 JSON 不崩）；校验（细纲声明补回/池外剔除/文本命中补回/≤6 裁）；落盘 `memory/castings/v{vol}-c{ch}-e{idx}.json`；needs → 工厂队列（source=broadcast） | ✅ |
| orchestrator 接线 | 事件循环注卡前 +1 次广播；`broadcast_casting: bool = False`（v1 默认关）；成功 → 名单驱动 cast、`ProductionResult.broadcasts_built` 回传；任何异常 → 静默回退确定性选角（与 ADR-020 同纪律） | ✅ |
| 测试 | `tests/test_broadcast.py` 28 条：池过滤/解析纪律/校验四场景/落盘/needs 入队/失败纪律(挂·blocked·垃圾·None)/orchestrator 集成（开启广播名单驱动 + 垃圾降级不阻生成）+ **F7 不在场点名 4 条**（真自造名仍拒 / 注册角色放行不进物理 cast / 集成 / 池排除前置） | ✅ 28 passed（含 test_broadcast_alias_match） |
| 文档 | ADR-021 状态改"已实施"；4 项拍板记录（细纲不可删 / 新人必经工厂 / 独立成次 / 落盘） | ✅ |

**验收口径**：v1 默认关（与 ADR-020 四件套同哲学——先默认不改变产出，harness/CLI 显式开启，
真机验证稳定后转 True）。开启后跑批需比对"广播名单 vs 细纲/文本兜底名单"的差异是否真的
减少了"职能上该在场的人缺席"（前两章归因 A 类主因）。

> **内部四件套 · 广播转正前置项（2026-09-06）**：F7 不在场点名已闭环（两级校验——
> 注册角色不在池放行为 `off_scene` 引用、真自造名仍拒；`names` 只回物理在场者，不进 N3
> 调度/正文 cast，见 `docs/问题总账` F7）。**广播转正已闭环（2026-09-06）**：用户规则
> 全局禁用 pro、只用 flash（`deepseek.py` 默认 `deepseek-v4-flash`）；广播 `max_tokens`
> 8000→16000 兜住 flash 更大思考离散（超大池 4-8K，偶发冲超被截断致 content 空）。以
> flash+16000 重跑 `broadcast_batch_probe`（18 事件）**broadcast_fired 18/18、degrade=0、
> miss=0、增益 21 全 plausible** → `broadcast_casting` 默认 False→True（详见 docs/问题总账 B1）。
> 另三件套（dp-microbeat 微拍 / dp-seam 状态锚接缝 / dp-intent 欲望着色）见 ADR-028 后各里程碑。

### M3t+ — 线索（Line）子系统批1（ADR-025，2026-09-06 拍板 / ✅ 代码+测试完成）

lingyu5 真机归因的第三根主因链：线索在账上但生成时看不见（宗门暗线 5 章 3 次名词提及、
零场景）。设计全档 `docs/线索子系统设计与规划-2026-09-06.md`，本批为阶段1-4 完整闭环。

| 件 | 实现 | 状态 |
| --- | --- | --- |
| 数据层 | `core/lines.py`（账本读写/确定性校验/分档冷却/四级视图/动作落账/提名转正/行解析）+ `schemas/bible/lines.schema.json`；缺文件=空账本降级 | ✅ |
| 规划侧 | book 节点 lines 骨架登记（主线唯一=硬校验 raise 重试）；卷纲五元组 arc（outcome 允许受挫）+ line_plan + 活跃支线预算告警；章纲 lines_present 生成 + 细纲 md 行内携带（render_gist_md）+ 确定性告警（收尾禁 open/开篇 hidden 禁揭开/冷却/悬空/死线复活）；sync_bible 导出 bible/lines.json（运行态合并 keep_extra=True） | ✅ |
| 生成注入 | prompt_budget 新增 `lines` 钉死层（-1）；事件层命中线卡（章纲声明优先+词元命中，≤3 条，≥2 条自动交织标注）——不再依赖 RAG 命中；润色 global_context 线索禁令（<50 字，账本空不加） | ✅ |
| 回写闭环 | Chronicler 抽取 prompt 第四部分线索行（账本注入块，账本空则跳过，不加调用）；Extraction.line_rows 具名字段（不改签名）；apply_extracted_rows：开/推/闭/交织/反转 + 账本外 id 提名 pending + 计划外闭合 closing_candidate + 死线复活拦截 + progress 追加去重；ChroniclerReport 三字段回传 | ✅ |
| 字数下限 | build_chapter_context 注入下限=目标 85%（"下限不是目标，严禁注水独白"——2026-09-06 用户拍板改 2026-09-05"不注入字数"决定；欠写 48-82% 实证托底）；system prompt 仍无目标值（源码级守卫保持） | ✅ |
| 测试 | `tests/test_lines.py` 20 条：账本校验/冷却分档/单行卡/章纲视图/动作落账/死线复活/提名转正/事件视图命中与交织/Chronicler 行解析/规划侧硬闸/sync_bible 运行态合并/细纲 md 往返/钉死层/下限注入/schema 合法性 | ✅ 20 passed，全量 **765 passed / 3 skipped** |

**遗留到批2（阶段5-6）**：卷中检查点（~50% 主线节自查）、卷末到期审计+篇幅比告警+yield
校验落 reports、一致性引擎接死线复活拦截、revise 通道转正、真机验收（用户新需求 5 章
按 7 项清单）。云服务器关闭期间本批未做任何依赖远程模型的验证（单测全走 fake provider）。

### M3t++ — 线索（Line）子系统批2（ADR-025 阶段5，2026-09-06 ✅ 代码+测试完成）

外部方法论（线索篇/伏笔篇）对比吸收后的三项借鉴随批2落地；全部确定性零 LLM，
账本空=整批跳过（降级纪律不变）。

| 件 | 实现 | 状态 |
| --- | --- | --- |
| 卷中检查点 | produce_chapter 4.4c：本卷 ~50% 章（ch==K//2）自查——active/suspended 线本卷零推进 → 写 `due` 强制处理项；chapter_view 置顶告警 + 卡片"强制推进"标注；章纲动作/正文回写触及即清算；closing_candidate 堆积/提名待转正进报告（soft_failures） | ✅ |
| 卷末审计 | volume_audit（卷末章触发）：threads target_vol 到期未回收 → 运行态写 `due` + 下卷卷纲"伏笔到期强制项"注入；已回收+carrier 伏笔 → register_pending 提名升级为线（人审转正）；本卷闭合线 yield 缺失告警；主线本卷进度占比 <30% 告警；报告落 reports/lines-audit-vol{N}.md | ✅ |
| 伏笔↔线索衔接 | threads schema（蓝图+运行态）加可选 carrier；_apply_threads 归一非法值；卷末审计提名复用 confirm_pending | ✅ |
| dormant 回声 | echo_warnings：dormant subplot/hidden 静默超 max(8,K) 章 → 建议 flicker 轻提及（重置冷却，不推进不揭真相）；接 chapter_view | ✅ |
| 埋设式写法 | 事件层线卡：开启章首场自动附"埋设式出场（轻淡带过，只写表象，不渲染其价值）" | ✅ |
| revise 通道转正 | replay_chapter_lines + CLI `forge lines-replay V C`：人工改细纲「本章线索:」后重放——旧声明本章痕迹确定性回滚（open→dormant/进度行删除/本章闭合撤销），新声明重新落账 | ✅ |
| 测试 | tests/test_lines.py +8（回声/检查点写清 due/审计 due+提名+yield+篇幅比/replay 回滚重放/埋设提示/卷纲 due 注入/schema） | ✅ 28 passed，全量见提交说明 |

### M3u — 商讨轮扩展：pace/romance/opening 三维度（2026-09-06 ✅ 代码+测试完成）

用户提出"开场的询问讨论可以再详细一些"，拍板：① pace/romance 升 required；
② 感情线模式联动线索账本。每个新维度绑定明确消费端（防 threads_involved 式死数据流）。

| 件 | 实现 | 状态 |
| --- | --- | --- |
| 新槽位 | `meta.pace`（required，轮1）/ `meta.romance`（required，轮4）/ `meta.opening`（recommended，轮4）/ `power_system.ceiling`+`worldview.map`（轮2）/ 主角 `flaw`（轮4）；反派 ask 文案加"动机一句话"；love_interest ask 提示"无CP 可跳过" | ✅ |
| schema | 蓝图 meta 加 pace/romance/opening（enum，不 required——不破坏旧蓝图校验）；power_system.ceiling / worldview.map / characters.flaw | ✅ |
| 消费端 | book prompt 注入三维度（endgame 同款死数据流防御）；volume prompt `_pace_section`（苟住发育→前两卷"守住即胜"等 outcome 语义）；chapter prompt `_chapter_meta_rules`（前三章开篇指令 + 感情线 cast 约束：无CP 禁感情戏/后宫多线并行不收敛）；`_lines_block_for_volume` 感情线登记建议（单女主/后宫 且账本无感情线 → 建议 line_plan.open 登记 ln:romance，纳入冷却/检查点管束）；chapter 人物卡补 flaw 字段 | ✅ |
| 测试 | tests/test_pace_romance.py 9 条（schema 枚举/旧蓝图兼容/槽位层级/detect_gaps 覆盖/注入 helpers/感情线联动）；test_m12 缺口数 19→25 | ✅ |

### M3v — 事件级实然回写完全体（2026-09-06 ✅ 代码+测试完成，ADR-013 落地闭环）

fame5 真机 10 章人审暴露修为/载体/数值三处穿帮，归因发现：worldstate 的事件级回写
（apply_delta/_apply_time）早已存在，但**读取侧从未接入事件 prompt**，且事件间先忆被
exclude_src 整章排除（H12 防复述的副作用）——下一事件只能看到冻结的角色卡+细纲两个
互相矛盾的计划态来源。用户拍板：**不做平行状态卡，所有回写以事件为单位，全部信息实时更新**。

| 件 | 实现 | 状态 |
| --- | --- | --- |
| 写侧·角色卡实然同步 | `worldstate.apply_delta` realm 变更时同步 characters.json 的 `power.level`（`_sync_card_realm`）；**仅合法推进才同步**（parse_realm 单调比较），倒退/体系外文本不动卡；无境界表放行 | ✅ |
| 写侧·基线快照 | worldstate 新增 `baselines`（init_from_bible 首次快照，setdefault 防覆盖）；角色卡从此可被实然同步而不破坏 R-STATE | ✅ |
| 检测锚迁移 | rules.py R-STATE 单调性基线：`baselines` 优先，旧项目 fallback 卡（向后兼容） | ✅ |
| 读侧·实然状态块 | orchestrator `_live_state_block`：每事件 prompt 构建时实时读盘，复用 `worldstate.snapshot_lines` 输出本场 cast 的修为/位置/持物/伤势行，钉死层（prompt_budget `live_state: -1`）注入，附"实然优先于计划态"仲裁指令 | ✅ |
| 读侧·先忆放开 | 事件级先忆 `exclude_src` 不再排除本章（事件 i 检索含事件 i-1 摘要）；复述防线=strip_seam_overlap+seam_review+【相关前情】"禁止复述"纪律 | ✅ |
| 读侧·角色卡事件级重载 | bible_chars 从章级一次读取改为每事件重载（上一事件回写已更新 power） | ✅ |
| 测试 | tests/test_event_sync.py 10 条（推进同步/倒退保卡/体系外拒同步/快照不可变/R-STATE 倒退+越级检测在卡同步后仍生效/实然块注入与空态）；全量 791 passed | ✅ |

遗留：重场戏 beat 展开路径（`_generate_beats`）的 prompt 未传 live_state（fame5 细纲 beats 全空，
低优先）；`_sync_card_realm` 只同步 power.level，location 等依赖实然块注入（卡上无此字段）。

**双 JSON 清污（P0 收尾，2026-09-06 同日闭环）**：fix_fame5_lines.py 写回未剥旧 frontmatter 导致
10 份细纲双段污染（总账 F8）。`bible.normalize_gist_frontmatter`（保留第一份回填段，标准 `---`
引导段与裸 JSON 段两种第二段均识别，正文保真）+ `normalize_all_gists` 产品化落地；
fame5 10 份实测干净且幂等（fixed=0），tests/test_gist_normalize.py 5 条，全量 **796 passed**。

### M3w — 承诺账本 + 未来窗口滚动细纲（ADR-026，2026-09-06 ✅ 代码+测试完成）

> 设计：ADR-026 + docs/10 §7.8。把"恒定 vs 可变"边界做成确定性承诺账本，并为"写到卷内某处后
> 纠偏未来 2-3 章细纲"提供 `forge roll-window` 入口——未触及承诺自动落盘，触及则回滚+人工闸门。

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| W1 | `forge/covenant.py`：承诺账本（threads 在途伏笔 / volumes 卷主线 / characters 核心人设，guard 字段）+ `touched_entries` 确定性触碰判定 + `affected_modules` 映射 + `summary_lines` 摘要 + 账本快照 | ✅ |
| W2 | `engine.roll_window`：窗口起点自动定位 / 宽度 / 越卷尾钳制 / 已写章跳过 / 承诺门（未触及自动落盘、触及回滚+`mark_pending`）/ 整卷写完提示衔接下一卷 / 滚动前快照 | ✅ |
| W3 | CLI `forge roll-window`（`--from/--width/--max-calls/--gate`）+ `forge covenant` 视图 + 导出 `roll_window`/`RollWindowResult` | ✅ |

**落地记录（2026-09-06）**：

- `forge/covenant.py` 全确定性、零 LLM；`_threads_entries` 只收 `planted/pending_return`
  在途伏笔（未承诺/已回收不构成承诺）。
- `roll_window` 门控语义：改动先过 `touched_entries` → 未触及自动落盘（细纲 md + 蓝图 chapters）；
  触及 → **回滚**窗口改动 + `mark_pending` 人工闸门（复用 ADR-024 分模块审核），绝不在窗口修订里
  静默改承诺；`gate=False` 供脚本/测试直跑。
- 边界：`--from` 缺省=卷内第一未写章；窗口越卷尾钳制在卷内；已写章跳过；整卷写完提示
  `forge roll <vol+1>` 衔接下一卷；每次滚动前自动打快照（F5 双快照基线）。
- 测试：`tests/test_covenant.py`（13 例）+ `tests/test_m21_roll_window.py`（7 例，全 ScriptedProvider）——
  覆盖自动落盘/门不误触发/起点定位/已写章跳过/卷写完结提示/触碰守卫生效/CLI 注册，全量回归通过。

### M3x — 记忆 LLM 侧选 rerank（ADR-027，2026-09-06 ✅ 核心已编码）

> 借鉴 Claude Code s09：在 `MemoryRetriever` 相似度打分之上叠一层可开关的 LLM 相关性精审，
> 补回"分词不重合但语义相关"的漏检碎片。核心机制落地，编排层 LLM 注入待真机验证阶段接线。

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| R1 | `MemoryQuery.reranker/rerank_pool` + `MemoryRetriever._rerank` + `MemoryHit.reason`；候选池放大(top_k×3)救回漏检；异常回退纯相似度 | ✅ |
| R2 | 测试 `test_m22_mem_rerank.py`：默认关闭零触发 / 语义相关救回 / reason 只写入选 / 异常回退 / pool 钳制 | ✅ |
| R3 | 编排层 LLM-backed 注入（判断类·thinking 路由）+ CLI 实验开关 | ⏳ 待真机验证阶段 |

**落地记录（2026-09-06）**：
- `memory.py`：`MemoryQuery.reranker` 为 `Callable[[str,list[dict]],list[tuple[sig,reason]]]`
  注入回调（默认 None=逐字节等价旧版）；`MemoryRetriever._rerank` 在打分排序后重排；
  rerank 异常静默回退（增强层不拖垮检索）；候选池外按原相似度回补。
- 设计取舍记 ADR-027；同 07 §7.1 检索说明已补。测试 5 例新 + memory 22 例回归全绿。

### M3y — 任务级持久化 + 编排器指派 owner + can_start（ADR-028，2026-09-06 ✅ 核心已编码）

> 借鉴 CC s12 Task System，适配 Novelist 中央编排：把卷→章落成细粒度任务 JSON，支持 owner
> 指派、can_start 依赖就绪、崩溃后单任务恢复。核心层就绪；orchestrator 推进卷/章的接线
> 与真机验证阶段结合做（避免在 docs/11 §13 的高风险整改区叠债务）。

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| T1 | `core/tasks.py` `Task`+`TaskStore`：任务 JSON 落盘/装载、状态机、owner 指派防重入(`TaskBusyError`)、can_start 依赖就绪、recover(list/redo)、precheck 承诺门衔接 hook | ✅ |
| T2 | 测试 `test_m23_tasks.py`：持久化往返 / owner 指派+幂等 / 异主防重入 / 依赖解锁 / recover list+redo / precheck blocked | ✅ |
| T3 | orchestrator 卷/章推进接线 + 崩溃启动 recover + covenant precheck 注入 | ⏳ 与真机验证一起 |

**落地记录（2026-09-06）**：
- `core/tasks.py`：文件名做 Windows 安全替换（逻辑 id 保留 `:`）。`start` 编排器指派 owner；
  `can_start` 只做依赖就绪（确定性）；`precheck` 注入承接 covenant gate（不内置 LLM）。
- 测试 8 例全绿。ADR-028 见 docs/03；docs/06 §4.1 数据设计已补任务层。

### M4 — 硬化与评测（持续）
- 完整评测集（见 09）与回归，含"记忆自洽 / 人设保真"专项（A7/A8）。
- 多个 Provider 实测（云 + 本地 Ollama/vLLM），含 Embedding 能力矩阵。
- 长文（≥20 章）全流程压力测试与一致性/记忆检索命中统计。
- **事件实时回写闭环**：事件落定即回写 memory + 下一书写点可先忆（NFR-13/A9）。
- **审核拦截降级**：MODERATION_BLOCKED 识别 + 改写/切换/人工链 + 敏感词预检（ADR-015/A10）。
- 稳定性/降级路径覆盖（含无 Embedding 时检索降级、审核拦截恢复）。

> 里程碑以"可演示的纵向切片"为单位（每次都有可跑通的新能力），而非单纯横向铺层。

### M5 — 多厂商 LLM 接入：api-key 统一管理 + 全量主流模型（2026-09-01 用户新增需求）

**需求**：系统可接入多家 LLM 厂商的 api-key，用全量主流大模型（国内外云厂商 + 本地）进行小说撰写——
生成/提炼/候选/抽取全流程均可在厂商与模型间切换，不绑定某一家。

**为什么单列**：现有 `providers/` 仅 openai（OpenAI 兼容）/ deepseek / lmstudio / fake 四家，其中
openai 与 deepseek 均为 OpenAI 兼容 SDK，无 Anthropic / Gemini / 智谱 / Kimi 等原生厂商适配器；
`ProviderConfig`（config.py:15-22）无 api-key 字段，密钥散落在环境变量、无统一管理；
且 docs/11 P0-2 记载 `REGISTRY`（providers/__init__.py:16-57）是死代码——`cli.py:354
_make_cli_provider` 用 if/elif 自行装配，未走注册表。多厂商接入必须先清偿 P0-2（Provider 双轨制）。

**排序**：排在当前未做完工作（M3l F3–F5、M3m T4、M4、docs/11 §13 P0/P1 整改清单）之后，不插队。

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| X1 | **Provider 统一装配**：清偿 P0-2（REGISTRY 单轨 + `providers.create()`，删 base.py 仅 docstring 文件）+ `ProviderConfig` 加多厂商密钥段（`api_keys: {name: key}`，密钥 gitignored + .env 加载，不入库） | 待做 |
| X2 | **原生厂商适配器**：Anthropic（Claude）/ Google（Gemini）/ 智谱（GLM）/ 月之暗面（Kimi）各实现 `LLMProvider` Protocol（docs/07 §2.3/§2.4）；OpenAI 兼容厂商（DeepSeek/通义/OpenRouter 等）复用 `OpenAICompatibleProvider` + base_url，零新增适配器 | 待做 |
| X3 | **能力矩阵与运行时切换**：模型能力注册（上下文长度/思考型标记/embedding 支持/成本档，本地 9B 思考关不掉的教训入库）；`provider@model` 语法；CLI `--provider` 覆盖全部命令；primary→fallback 失败切换链；embedding provider 独立可配 | 待做 |
| X4 | **验收**：≥3 家真实云厂商各跑通 ≥1 章；同一本书运行中切换厂商续写不丢上下文（bible/细纲/记忆已落盘，切换只换生成后端）；密钥经 gitignored 配置注入 | 待做 |

验收：A11（多厂商可用，docs/02 §5）；NFR-2（Provider 插件化）实跑兑现。

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
| R14 | 多厂商接入：密钥泄露 / 厂商接口行为差异（响应字段、限流、审核拦截码） | 中 | 密钥 gitignored + .env 加载、不入库；适配器统一归一化到 `LLMResponse`；拦截识别复用 F14 链路（M5） |

## 5. 实现顺序建议（TDD 取向）
1. 先 `storage`（工作区/schema/检查点）——是所有上层的地基。
2. 再 `providers`（抽象 + 1 个真适配器，含拦截识别）——打通 LLM。
3. 再 `core/agent_runner + tools/registry + permission`——打通 Agent 循环。
4. 再 `tools` 创作工具 + `pipeline`（串行逐章 + 事件回写）——打通一条完整工序。
5. 再 `memory`（读取→编纂→检索）——打通"先忆 / 后纂"，回填正文写作流程。
6. 再 `actor`（takes + 排演整合）→ `scene`（围读会 + 收敛判据）——打通人设生动化。
7. 再 `moderation`（预检 + 拦截降级链）——打通内容合规。
8. 最后 `consistency` 全量、HTTP、评测（含记忆/演员/围读/审核维度）。
9. **M5 多厂商接入殿后**（用户 2026-09-01 新增）：先清偿 P0-2（REGISTRY 单轨），再按 X1→X4 扩展，
   排在 F3–F5 / T4 / M4 之后。

## 6. 后续可迁移点（Backlog 对接）
- worker 进程池化（分布式）、多项目空间、文风学习、内容回流、跨书记忆迁移（对应 02§6）。
- 工程实现细化：`core/memory.py`（记忆子系统 + RAG 索引）、`agents/*.prompt`（含角色演员提示词）、`schemas/memory/*.schema.json` 的落地节奏见 M2/M3 的评测部分与本文 §3 路线节点的对应。

> 质量、评测、一致性保障的完整内容见独立文档 `docs/09-quality-assurance.md`。
