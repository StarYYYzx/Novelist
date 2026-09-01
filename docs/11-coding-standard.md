# 11 — 编码规范（CodING Standard）

> **本文件的读者是编码 agent 与后续贡献者**：与 docs/01–10 一同构成开发契约。
> 规范分两部分：§1–§12 是**新代码必须遵守的规则**；§13 是**存量代码整改清单**（含具体改法与优先级）。
> 制定依据：2026-09-01 全库体检（8047 行 src / 4128 行 tests / 376 个函数）。

---

## 1. 总原则

1. **文件是事实源，代码是加工器**（ADR-016）：一切持久状态落在 `novel_workspace/<proj>/` 下的
   JSON/Markdown，代码只做读→算→写。SQLite 与向量索引是可再生缓存。
2. **确定性优先**：能用规则/正则/查表解决的（阶段判定、量词归一、分档提醒、V6 检查）不调 LLM；
   LLM 只用于真正的语义判断（生成、抽取、改写）。
3. **单测绝不真调 LLM**（docs/09 §2.1）：一切测试用 `providers/fake.py`（FakeProvider / ScriptedProvider）。
4. **改 schema 必须同步**：`schemas/**` 改动须同步 docs/06 §5.2 规则引擎白名单，并在测试中断言
   SchemaRegistry 能校验新格式（F0' 起有文件级+条目级双层 schema）。

## 2. 分层与依赖规则（强约束）

现状分层（体检确认无违规，保持）：

```
cli.py / server.py          # 入口层：装配 + I/O，不含业务规则
  └─> core/                 # 内核：业务规则与编排
        └─> storage/        # 持久化：workspace/checkpoint/indexdb/models
  └─> tools/                # 工具实现：依赖 core.tools 抽象（Tool/ToolResult/门禁）
  └─> providers/            # LLM 适配器：只依赖 core.llm 的 Protocol
  └─> consistency/          # 规则引擎：读 core + storage，不写业务状态
```

规则：

| 规则 | 说明 |
| --- | --- |
| **R2.1 依赖只能向下** | `storage/` **禁止** import `core`；`core/` 禁止 import `consistency`、`providers`（provider 通过 Protocol 注入，见 §9） |
| **R2.2 入口层薄** | `cli.py`/`server.py` 只做：参数解析 → 装配 → 调 core → 输出。业务判断不出现在入口层 |
| **R2.3 相对 import** | 包内一律 `from .x import y` / `from ..core import z`；禁止绝对 `from novelist.` |
| **R2.4 延迟 import 仅限两种情况** | ① 打破启动成本（cli 顶层）；② 打破循环依赖且已在 §13 清单登记。其余一律顶部 import |
| **R2.5 新包先写 `__init__.py` docstring** | 一句话说明职责与对应 docs 章节（现有 forge/ 的写法为准） |

## 3. 命名规范

| 对象 | 规则 | 示例 |
| --- | --- | --- |
| 模块/文件 | `snake_case`，名词，一个模块一个职责 | `worldstate.py`、`entity.py` |
| 类 | `PascalCase`；数据载体用 `@dataclass`，服务/网关类可用普通 class | `PhasePolicy`、`Chronicler` |
| 函数 | `snake_case` 动词开头；私有 `_` 前缀 | `payoff_checklist()`、`_truncate_to_boundary()` |
| 常量 | `UPPER_SNAKE` 模块级 | `PIPELINE_STAGES`、`QUANTIFIER_TABLE` |
| 一致性规则 | `R-<域>` 大写 | `R-STATE`、`R-THREAD`、`R-TIME` |
| 实体 id | 小写前缀 + 冒号 | `char:yelan`、`pt:V017`、`set:xisuidan`、`tl:13`、`pd:1` |
| LLM 抽取行 | `<域>：值` 全角冒号单行 | `时间：+90日`、`约定：叶蓝出关｜+90日`、`状态：叶蓝 闭关中` |
| 测试文件 | `test_<模块名>.py`（新测试）；里程碑遗留名 `test_m0..m9` 不强制改 | `test_phase.py`、`test_entity.py` |
| 测试函数 | `test_<行为>[_<条件>]` | `test_tail_phase_pays_off_threads` |

**约定俗成的域词**（新代码沿用，不再发明同义词）：`bible`（应然设定）、`memory`（实然记忆）、
`blueprint`（构建中间态）、`pending`（定时事件）、`thread`（伏笔）、`stage`（实体引入四阶段）、
`phase`（开篇/行文/收尾）。

## 4. 代码组织

| 规则 | 上限 | 超限处置 |
| --- | --- | --- |
| **R4.1 函数长度** | 50 行（不含 docstring/注释）；编排壳函数可放宽到 80 | 拆分为同模块私有函数；拆分后各自可单测 |
| **R4.2 参数个数** | 位置参数 ≤5；关键字参数 ≤12 | 超过即引入 `@dataclass` 参数对象（见 §13 P0-1 的 ChapterJob） |
| **R4.3 模块行数** | 500 行 | 按职责拆模块（orchestrator 已超限，见 §13） |
| **R4.4 单函数单职责** | 编排函数 = 顺序调用子步骤 + 组装结果；子步骤各自私有函数 | produce_chapter 是反例与整改样板 |
| **R4.5 注释写"为什么"** | 分段注释 `# ---- N) 阶段名 ----` 的写法保留 | 拆函数后标题进函数名，注释只留决策理由（含人工审查批次引用） |
| **R4.6 魔法数字必须具名** | 阈值/配额/倍率进模块级常量或 dataclass 默认值 | 禁止行内裸数字（`* 1.8` 这类已有注释的例外需登记） |

## 5. 类型标注与 docstring

- **R5.1 新代码 100% 标注**：所有函数（含私有）参数与返回值必须有类型；`ws: Workspace`、
  `provider: LLMProvider` 不许裸奔（现状 46 个函数无返回类型、83 个参数未标注，全部在 §13 清偿范围）。
- **R5.2 容器精确**：`dict[str, Any]` 仅允许出现在 **JSON 边界**（读写 workspace 文件、LLM 返回解析）；
  跨函数传递的数据用 `@dataclass` 或 TypedDict。
- **R5.3 docstring**：公共函数/类必须有（一句话职责 + 关键参数语义 + 对应 docs/ADR 引用）；
  私有函数可省略但复杂逻辑须有行内注释。现状 56% 缺失，增量补齐，改哪个函数顺手补哪个。
- **R5.4 `type: ignore` 必须带规则码**（现状已做到，16 处，保持）。
- **R5.5 `from __future__ import annotations`**：新模块一律加（现状一致）。

## 6. 数据访问

- **R6.1 禁止调用 `Workspace._abs()`**（私有）。用公共 API `ws.path(*parts)`（§13 P0-3 新增）。
  现状 21 个文件 42 处外部调用 `_abs`，是最大的封装泄漏。
- **R6.2 一切写盘走 `Workspace` 的原子写**（`write_text`/`write_json`），禁止裸 `open(...,'w')` 写工作区文件。
- **R6.3 bible 访问模式**：读 JSON → `SchemaRegistry` 校验（条目层）→ 内部转 dataclass/TypedDict。
  禁止裸 `bible["style"]["tone"]` 连环取值散落多处——集中在 `core/context.py` 的 load_bible 出口。
- **R6.4 upsert 语义**：按 id 合并的文件（characters/plot_threads/settings）统一走
  "读→按 id upsert→原子写回"，禁止整文件覆盖式追加。

## 7. 错误处理

- **R7.1 统一异常根**：所有项目异常继承 `core/errors.py` 的 `NovelistError`。
  现状 7 个孤儿异常（直接继承 Exception）在 §13 P1-4 归位。**新增异常禁止直接继承 Exception**。
- **R7.2 异常携带位置**：消息含 `(vol, ch)` 或文件路径，供 CLI/报告定位。
- **R7.3 禁止裸 `except:`**（现状 0，保持）；`except Exception` 必须带 `# noqa: BLE001` + 理由注释
  （现状已做到，保持）。
- **R7.4 可恢复问题走 alerts，不走异常**：生成截断、配额告警、实体推迟等记入
  `ProductionResult.*_alerts` 由上层决定呈现；异常只用于"这一章写不下去了"。
- **R7.5 CLI 边界转换**：入口层捕获 `NovelistError` 转 `click.ClickException`；其余异常裸抛（带栈）。

## 8. LLM 调用规范

- **R8.1 一切调用走 `LLMRequest`/`LLMProvider` Protocol**（core/llm.py），禁止在业务代码里直接
  `httpx.post`/`openai.chat`。
- **R8.2 预算语义**：`max_tokens_out`=总预算，`max_content_tokens`=期望正文量；适配层
  `budget = max(总预算, 正文预算)`。思考型模型（qwen3.5-9b）思考计入总预算——本地跑批经验见
  docs/09 与 AGENTS.md，**生成单位按事件不按整章**。
- **R8.3 JSON 返回解析**：prompt 末尾加"只输出 JSON"约束；解析失败重试 1 次→回退父层产物并记 warn
  （Forge 引擎通用降级策略）。
- **R8.4 抽取行协议**：编纂员/ingest 的 LLM 输出按"域：值"单行协议解析（§3 命名表），解析器集中
  在 chronicler，禁止各处手写正则。

## 9. Provider 与扩展点

- **R9.1 单轨注册**：新增 provider = 实现 Protocol + 在 `providers/__init__.py` REGISTRY 注册工厂
  （一处）；**禁止**在 cli 里加 if/elif 分支（现状双轨制是债务，§13 P0-2 整改）。
- **R9.2 测试替身**：`FakeProvider`（固定回复）/ `ScriptedProvider`（脚本化工具调用序列）覆盖全部
  单测需求；新增 provider 行为必须同步补 fake 的等价能力。
- **R9.3 Genre Pack 是数据不是代码**：新类型 = `forge/genres/*.json` 一个文件，禁止为此改 Python。
- **R9.4 一致性规则可插拔**：新规则 = `consistency/rules.py` 加 `R-XX` 函数 + 注册到 `run_rule_checks`
  + docs/06 §5.2 白名单同步。

## 10. 测试规范

- **R10.1 不真调 LLM**（§1.3）；需要 embedding 的用 keyword-fallback 或 fake 向量。
- **R10.2 共享夹具进 `tests/conftest.py`**：`workspace`（tmp_path 下的空项目）、`bible_project`
  （含最小可写 bible 的项目）、`fake_provider`。现状 19 个文件各自手搓，§13 P1-5 收口；新测试
  **必须**用 conftest 夹具，禁止再复制 setup 代码。
- **R10.3 断言用语义字段**（`res.bible_injected is True`），不断言整段文本相等（脆断言）。
- **R10.4 慢测试标 `@pytest.mark.slow`**（真实 LM-Studio/API）；默认集合必须全绿。
- **R10.5 行为命名**：新测试文件按 `test_<模块>.py`；里程碑遗留文件（test_m0..m9）新增用例时
  迁到对应模块文件，旧用例不强制搬家。
- **R10.6 修复 bug 先写复现测试**：commit 里测试先行（现状惯例，保持）。

## 11. 工具链与提交

- **R11.1 提交信息**：conventional commits + 中文摘要——`feat(<scope>): …` / `fix(<scope>): …` /
  `docs: …` / `test(<scope>): …`；scope 用模块名（entity/budget/phase/orchestrator/forge…）。
- **R11.2 ruff**：pyproject 配置见 §13 P2-9；新增代码必须过 `ruff check`；
  行宽 100（现状最长 124，存量超宽行逐步收）。
- **R11.3 mypy（宽松起步）**：`ignore_missing_imports = true`，先只对 `core/` 新增文件启用，
  存量按模块逐步纳入。
- **R11.4 死依赖零容忍**：声明进 pyproject 的依赖必须被使用（现状 structlog 零使用，§13 P2-8 清理）。

## 12. 文档同步义务（编码 agent 的收尾清单）

每次功能提交前自查：

1. 改了 `schemas/**` → 同步 docs/06 §5.2 白名单 + 相关 schema 校验测试；
2. 新增机制/横切关注点 → docs/03 是否需要新 ADR；改动现有机制 → 对应 ADR 是否要标注修订；
3. 新增模块 → AGENTS.md 代码层清单 + docs/08 里程碑表；
4. 里程碑推进 → docs/08 状态列；
5. 用户可感知行为变化 → docs/07 接口（CLI 参数/HTTP）。

---

## 13. 存量整改清单（按优先级）

> 体检方法：AST 扫描（函数长度/类型/docstring）+ grep（封装泄漏/死代码/依赖方向）。
> 执行原则：**伴随功能开发渐进整改，不搞一次性大重构**；每项标注"契机"（哪个功能动手时顺路做）。

### P0（结构性债务，阻碍扩展）

**P0-1 拆解 `produce_chapter`（497 行、25+ 参数，orchestrator.py:783）**

契机：M3m T2（临近事项注入）动手时——不拆则新注入只能继续往巨函数里塞。

改法：
1. 新建 `@dataclass ChapterJob`：承载全部 25 个参数（生成预算、篇幅治理、质量开关、事件循环开关），
   `produce_chapter(job) -> ProductionResult` 签名塌缩为 1 个位置参数；
2. 按既有分段注释拆私有步骤函数（各 ≤80 行，编排壳只做顺序调用+结果组装）：
   - `_load_inputs(job)` → 细纲/圣经/JIT 补卡（含 §"1)"段，现 :844-870 附近）
   - `_decide_phase(job, bible)` → 阶段判定（"1.5)"段）
   - `_generate(job, …)` → 事件循环/直出/续写/剧本（"2)"段，最大块，可再按 direct/loop 分两个）
   - `_polish_and_cap(job, text)` → 润色+篇幅截断（"4)"与"4.4)"段）
   - `_post_verify(job, text)` → 设定交代+实体进度（"4.5)""4.6)"段）
   - `_writeback_events(job, text)` → 编纂员回写（"5)"段）
3. 约束：**行为不变**——拆完跑全量 220 测试必须全绿，ProductionResult 字段不增不减（新增字段
   属于功能提交，不混入重构提交）。

**P0-2 Provider 双轨制归一（providers/__init__.py REGISTRY 死代码 vs cli.py:352 `_make_cli_provider`）**

现状：REGISTRY/register_provider/get_provider 定义后零调用方；CLI 用 if/elif 硬编码 5 个 provider。

改法：
1. `providers/__init__.py` 底部实际执行注册（fake/scripted/deepseek/lmstudio/openai 各一个工厂）；
2. `_make_cli_provider` 改为 `providers.create(name, **kw)`（查 REGISTRY + KeyError 转友好提示）；
3. 删除 `providers/base.py`（只有 docstring，8 行名不副实），注册表说明并入 `__init__.py` docstring；
4. 新 provider 只写一处。

**P0-3 Workspace 公共路径 API（`_abs` 被 21 文件外部调用 42 次）**

改法：`storage/workspace.py` 加公共方法 `def path(self, *parts: str) -> Path: return self._abs(...)`；
全库 `ws._abs(` → `ws.path(` 机械替换（sed 可完成，42 处）；`_abs` 保留私有供内部使用。
契机：任何动 workspace 的批次顺路做，或单独一个 refactor 提交（纯机械，低风险）。

### P1（一致性债务）

**P1-4 孤儿异常归位（7 个类直接继承 Exception）**

| 类 | 位置 | 归属 |
| --- | --- | --- |
| `AgentLoopError` | core/agent_runner.py:22 | `NovelistError` 直系 |
| `OAuthError` / `ModerationBlockedError` | core/llm.py:107/111 | `ProviderError` 子类 |
| `MemoryConflictError` | core/memory.py:39 | `NovelistError` 直系 |
| `PipelineStateError` | core/pipeline.py:14 | `NovelistError` 直系 |
| `WritebackError`/`ContradictionError` | core/writeback.py:45/49 | `NovelistError` 直系 |
| `CheckpointError` | storage/checkpoint.py:34 | `NovelistError` 直系 |

改法：改继承 + 全库 grep 捕获点确认无 `except Exception` 依赖旧层级语义（体检确认无裸 except，安全）。

**P1-5 tests/conftest.py 共享夹具（19 个文件各自手搓 Workspace/project）**

改法：新建 conftest，提供 `workspace`（空）、`bible_project`（最小 bible：worldview/characters/
style/volumes/细纲 1-1）、`fake_provider` 三个 fixture；新测试一律用；存量测试文件**不动**
（行为已验证，搬家纯风险）。契机：M3m T1 写测试时建 conftest。

**P1-6 CLI 装配去重（chapter/promote/export 等命令重复 gate+approvals+registry 装配）**

改法：cli.py 内 `_wire_runtime(ws, project_id, policy) -> RuntimeWiring`（dataclass：
gate/approvals/reg/emb），各命令一行取用。命令函数降到 ≤40 行。契机：P0-2 同批做。

**P1-7 类型与 docstring 增量补齐**

不安排专项；规则：**改到哪个函数，顺手补齐标注与 docstring**。新增代码 100%（R5.1）。
高优先补齐点：`produce_chapter` 拆解时（P0-1）顺路把 ChapterJob 及全部子函数标满。

### P2（卫生债务）

**P2-8 依赖清理**：pyproject 删 `structlog`（零使用）；pydantic 二选一——models.py 既然 try/except
可选导入，就从 dependencies 挪到 optional（或反向：删 try/except 当硬依赖）。推荐前者（现状实际
不依赖 pydantic 也能跑）。

**P2-9 工具链接入**：pyproject 追加：

```toml
[tool.ruff]
line-length = 100
target-version = "py311"
[tool.ruff.lint]
select = ["E", "F", "W", "I", "B", "UP", "SIM"]
ignore = ["E501"]   # 存量长行渐进收，先不 block
[tool.mypy]
python_version = "3.11"
ignore_missing_imports = true
check_untyped_defs = false   # 宽松起步，随 P1-7 收紧
```

dev 依赖加 `ruff>=0.4`、`mypy>=1.10`。

**P2-10 长函数跟进**（不阻断，各批次顺路）：`tools/memory_tools.py:31 tools()` 113 行（按工具拆
builder 函数）、`consistency/rules.py:280 _worldstate_check` 97 行（R-STATE/R-TIME 扩展时拆）。

### 整改登记表（防重复登记）

| 项 | 状态 | 契机 |
| --- | --- | --- |
| P0-1 produce_chapter 拆解 | 待做 | M3m T2 |
| P0-2 Provider 单轨 | 待做 | 任意批次 / 独立 refactor |
| P0-3 ws.path() | 待做 | 独立 refactor（机械替换） |
| P1-4 异常归位 | 待做 | 独立 refactor（低风险） |
| P1-5 conftest | 待做 | M3m T1 |
| P1-6 CLI 装配 | 待做 | 与 P0-2 同批 |
| P2-8/9/10 | 待做 | 顺路 |

---

## 附：体检数据基线（2026-09-01）

| 指标 | 数值 | 目标 |
| --- | --- | --- |
| src 总行数 | 8047（core 4640，最大文件 orchestrator 1280） | 单文件 ≤500 |
| 函数总数 | 376；>50 行 16 个；>100 行 2 个 | >100 行归零 |
| 类型缺口 | 无返回类型 46 / 参数未标注 83 | 新代码 0 缺口，存量增量清偿 |
| docstring 缺失 | 211/376（56%） | 公共 API 归零 |
| 裸 except / TODO / FIXME | 0 / 0 / 0 | 保持 0 |
| `ws._abs` 外部调用 | 21 文件 42 处 | 归零（迁移 `ws.path`） |
| 死代码 | providers REGISTRY、base.py | 归零 |
| 死依赖 | structlog（声明未用） | 归零 |
| 测试 | 22 文件 4128 行 220 用例，无 conftest | conftest + 行为命名 |
| 分层违规 | 0（storage 不反向依赖 core ✓） | 保持 0 |
| 提交规范 | conventional + 中文（10/10 合规） | 保持 |
