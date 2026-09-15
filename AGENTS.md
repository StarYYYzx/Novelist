# AGENTS.md — Novelist 仓库指南（AI 协作规范唯一来源）

> **本文件是规范的唯一来源。** `CLAUDE.md` 与 `.github/copilot-instructions.md` 只是指向本文件的薄桥接；
> Codex / Copilot / Cursor / Cline / Zed / Amp / Jules 原生读本文件。
> **通用规范只写在这里** —— 同一事实写两处必然漂移，那是本仓库历史上反复出现的病。
>
> 接手先读 `docs/13-ai-collab-guide.md`（开发要点：模型接入纪律、协作流程、待办优先级、已知坑）。
> **判断进度看代码与 `git log`，不要看 README/本文档的阶段自述**（会滞后）。

Novelist 是多 Agent 长篇小说撰写系统。设计文档先行，按 M0–M4 里程碑推进实现（M0–M2 已完成，M3 进行中）。

## 0. 接手第一步

```bash
python scripts/ai_bootstrap.py     # 装载技能（建目录联接）+ 挂 git hook —— 每个 clone 跑一次
python scripts/check.py --list     # 看看门禁有哪些
```

技能（Agent Skills）的**唯一源**在 `.ai/skills/`，随 git 分发；`.workbuddy/skills` 与 `.claude/skills`
是指向它的目录联接，由 `ai_bootstrap.py` 建立（**一份源、两处原生可见、零复制零漂移**）。
新增技能见 `.ai/skills/README.md`。

## 1. 提交前必须过门禁

```bash
python scripts/check.py --all      # 全部：ruff + 密钥 + 卫生 + pytest 全量 + 文档基线（约 90s）
python scripts/check.py --quick    # 只跑层A（<3s）；pre-commit 已挂这个
```

- CI（`.github/workflows/ci.yml`，**Python 3.11**）跑同一条 `--all`。**不要绕过门禁**；
  确需跳过 pre-commit：`SKIP_CHECK=1 git commit ...`，并在提交信息里说明原因。
- **沙箱环境变量由 `check.py` 自行注入**（`CODEBUDDY_SAFE_DELETE_ENABLED=0` /
  `CODEBUDDY_SAFE_DELETE_BULK_THRESHOLD=100000`）—— 不要再手工拼 pytest 命令。
  漏设会让测试自身的 unlink 累计超阈值被沙箱误杀，表现为"偶发失败、单跑全过"，是最难查的一类问题。
- **新增约定时优先加一条机械检查，而不是写一段文字。** 文本规范会腐烂，机械检查不会。
  门禁清单与"刻意不做"的项见 §7。

## 2. 命令

```bash
python -m pytest tests/ -q                                # 全量（建议直接用 scripts/check.py --all）
python -m novelist.cli --help                             # CLI（src 在 sys.path 上）
E:/python/ana/Scripts/ruff.EXE check src tests scripts    # ruff 不在 PATH 时用这个绝对路径
```

- 依赖见 `pyproject.toml`。**未做 editable install**；pytest 靠 `[tool.pytest.ini_options].pythonpath=["src"]` 注入。
- 默认 `addopts = -m 'not slow'`：**真实 API / 慢测试必须标 `@pytest.mark.slow`**，
  绝不允许混在默认套件里真调外部服务（CI 无密钥，会直接曝出来）。
- 新增 Provider 须实现 `core/llm.LLMProvider` Protocol + 在 `providers/__init__.py` 的
  **REGISTRY 注册工厂**（单一入口）；单元测试用 `providers/fake.py`，**绝不真调 LLM**（docs/09 §2.1）。

### 运行环境（Windows 本机）

- **装了 pytest 的解释器是 `C:/Python314/python.exe`**（托管 Python 3.13.12 未装 pytest）。
  `scripts/check.py` 用哪个解释器启动，pytest 子进程就用哪个（内部走 `sys.executable`）。
- 本机 git bash **缺 coreutils**（`head`/`tail`/`cat`/`wc`/`find`/`dirname` 全部 not found）→
  统计与过滤一律用 `python -c "..."`，不要写 shell 管道。
- `rm` / `Path.unlink` 被沙箱拦；清空文件用 `: > file`，删文件走 Python。
- **`git status` 显示 `[gone]` 是本机假象**：此环境无法在 `.git/refs/` 下建子目录，`refs/remotes/*` 不落盘
  （tag 与分支正常）。别当成远端丢分支。**禁 `git-filter-repo`**（会清空 `.git`）。

## 3. 只读 / 禁区

- **`tests/test_noval/` 是用户手写稿，只读不改删**（已 gitignore，不得提交）。
- **`.workbuddy/` 是项目记忆数据，不要删**（`.workbuddy/skills` 是联接，删它会删掉技能源）。
- **`core/scene_tools.py` 零引用但不要删**：`docs/05` §93/274/279 与 `docs/08` 明确记载它是
  "已备工具、尚未接入围读会"的**预留件**——删它要连带改设计文档，属设计决策，不是卫生问题。
- 工作区数据（`novel_workspace/`、`*.index.db`、`demo/`）不入 git；测试一律用 `tmp_path`。

## 4. 已拍板口径（改码前先对齐，别自作主张）

- **生成一律走 DeepSeek API `deepseek-v4-flash`（禁 pro）**。云端 AutoDL 与本地 LM-Studio 已停用删码。
- Provider 走**单轨工厂** `providers.create(name, **kw)` + PRESETS（openai/qwen/kimi/glm/anthropic/
  ollama/vllm/custom，见 docs/07 §2.4）。Key 优先级：显式值 > 环境变量 > gitignored `.env`。
- **单元测试绝不真调 LLM**（用 `providers/fake.py`）；真实 API 用例必须 `@pytest.mark.slow`。
- embedding = 本地 fastembed ONNX（nomic-embed-text-v1.5 / 768 维 / CPU 零 torch），不可用降级关键词。
  两坑：缓存落 `%TEMP%` 易被清；惰性加载会穿透构造期降级。预下载需清代理 + `HF_ENDPOINT=hf-mirror` +
  `HF_HUB_DISABLE_SYMLINKS=1`。
- `SceneBus` 用**不可重入** `threading.Lock`——持锁时不得再进加锁方法（`ApprovalQueue.wait_for_decision`
  的超时分支因此直接在锁内标记 deny，而不重入 `decide()`）。
- 关键决策：ADR-002 正文严格串行逐章 | ADR-011 bible=应然 / memory=实然 | ADR-013 事件落定即实时回写 |
  ADR-016 文件=持久事实源（SQLite/RAG 仅可再生缓存）| ADR-017/018 Forge 递归硬边界 | ADR-019 worldstate |
  ADR-021 事件级选角 | ADR-034 跨工具 AI 规范与技能分发。
- **3-E 拍板（2026-09-15）**：S-1 润色层术语硬约束**不做**（语言风格主观，非当前主要矛盾——
  上下文一致性才是）；S-2/S-3 世界观基座已统一注入（director 层 + forge 卷纲/细纲/人物卡节点，
  共享 `context.worldview_base_lines`，同一数据源同一措辞）；U7 已落地——`chapter` 的预算/
  管线开关/Agent 审查参数可写 `[generation]` 配置段，优先级**显式 flag > 配置 > 出厂默认**
  （`config.resolve_opt`），未知键报错。

## 5. 改动前的联动检查（漏了就是债）

- 写代码前读 **`docs/11-coding-standard.md`**（分层/命名/类型/错误/测试规则）；动
  `orchestrator`/`providers`/`workspace` 前先看它 §13 的存量整改清单，避免在债务上叠债务。
- **改 schema 必须同步审查 `docs/06` §5.2 的规则引擎白名单**，否则引擎不认识新字段。
- **新增机制要回填对应 ADR 与 FR/验收标准**（`docs/03` / `docs/02`）。
- **文档基线数字由 G5 门禁强制校验**：代码变更导致基线变动时 `check.py --all` 会红 ——
  请更新 `docs/11` 附:体检数据基线的实测列，**不要改脚本去迁就文档**。
- 文档里的"全量 774 passed"这类里程碑数字是**当时实况的历史记录**，不要"修正"；
  只有"当前基线"类声明才需要同步。

## 6. 代码与文档索引

- `docs/01` 概述 + "以写代码的方式写小说" | `02` 需求（FR/NFR/UC/验收 A1–A10）| `03` 架构选型 + ADR |
  `04` 总体架构 | `05` Agent 设计（主编剧/子代理/演员/编纂）| `06` 数据设计（工作区 + SQLite 索引）|
  `07` 接口 | `08` 实现规划（M0–M4/风险，含各里程碑完成状态）| `09` 质量 | `10` 构建层 Forge |
  **`11` 编码规范**（含存量整改清单 P0–P2）| `12` 测试计划 | `13` AI 协作开发要点。
- `docs/人工审查.md` 是用户的审查意见与待办（含空白的"第二批"，等用户填）。
- **带日期的报告类文档**（不在 01–13 编号内，属某轮工作的实证产物）：
  `问题总账-2026-09-03.md`、`质量加固提案-2026-09-05.md`、`流程异常排查-2026-09-05.md`、
  `测试记录-2026-09-06-*.md`、`线索子系统设计与规划-2026-09-06.md`、
  **`prompt审计-2026-09-12.md`**（提示词结构/成本审计，含 P0–P2 修复清单 + 批次 1 落地记录）、
  **`prompt组装结构审计-2026-09-12.md`**（逐环节信息覆盖审计：细纲层缺世界观基座、
  审校层三处窗口窄化、审校维度名与解析白名单不一致）、
  **`代码与逻辑复查-2026-09-15.md`**（三维复查：代码级缺陷 D1–D10 / prompt 组装覆盖度 /
  终端 UX U1–U8，含按风险排序的修复批次 3-A…3-D 与"需拍板"的 3-E）、
  **`Agent层审计与修复方案-2026-09-15.md`**（Agent/工具调用专项审计 AG-1…AG-24 + 四批修复方案；
  含"默认 mode=tool 不落盘"「工具定义从未下发」等 8 组离线复现与 6 项待拍板）。
  另有运行期证据：`novel_workspace/_harness/`（含 `prompt作用审计.md`「prompt 有没有接上链路」、
  `前两章问题归因报告.md`、以及实测导出的 prompt dump）。
- `src/novelist/`：
  - `core/`：`llm`（Provider 抽象）、`embedding`（Embedding + 关键词降级）、`memory`（索引/检索/写入）、
    `writeback`（事件实时回写）、`worldstate`（人物硬状态 + 时间轴/pending）、`tools`（注册表 + 三级门禁）、
    `approval`（审批队列）、`pipeline`（工序状态机）、`orchestrator` + `agent_runner`（Agent 循环）、
    `phase`（分阶段工作流）、`entity`（实体引入状态机）、`scene`（围读会总线）、`export`、`errors`、
    `events`、`moderation`、`calllog`（原始 LLM 调用日志，ADR-035：完整 prompt/请求体/原始响应 +
      上下文栈，落盘 `raw-calls/`）、`output`（进度/提示的**统一出口** `emit()` + sink 重定向 +
      耗时格式化：生成期输出不再裸 `print`，console/server 下可实时转发且有 UTF-8 兜底）、
    `normalize`（确定性文本/数据归一：禁令去重、术语清单拆分与归并——
      装配侧与 Forge 写入侧**共用同一份**）。
  - `forge/`：`state`（Blueprint/provenance）、`slots`（槽位与缺口）、`ask` + `io_console`（分轮商讨）、
    `seed`（模式一）、`ingest`（模式二）、`engine` + `nodes`（递归构建）、`genres`（类型包）、
    `validate` + `report`（契约校验与构建报告）。
  - `providers/`：`openai`（OpenAI 兼容基类，含审核拦截识别）、`deepseek`（生成通道）、各厂商 PRESETS、
    `fake`（测试替身）、`secrets`（.env + key）。
  - `storage/`：`workspace`（沙箱 + 原子写）、`checkpoint`（双轨快照）、`indexdb`（SQLite 辅助索引）、
    `models`（Schema 校验）。
  - `tools/`：`filesys` / `writing` / `memory_tools` / `governance`，经 `build_registry()` 装配。
  - `cli.py`（click）、`server.py`（FastAPI）、`config.py`（TOML 配置）。
  - **目录结构以真实布局为准**，`docs/08` §2 附有"原图条目 → 实际落点"对照表。

## 7. 门禁清单（scripts/check.py）

| 项 | 内容 | 层 |
|---|---|---|
| G1 | `ruff check src tests scripts` | A（<3s） |
| G2 | 密钥扫描：`sk-` / `ghp_` / 私钥块 + 敏感文件是否被 git 跟踪 | A |
| G3 | 卫生：根目录临时文件残留、`.gitignore` 关键条目、`test_noval` 未被跟踪 | A |
| G4 | `pytest` 全量（**强制注入沙箱环境变量**） | B（~90s） |
| G5 | 文档基线：重算 9 项指标并与 `docs/11` 基线表逐格比对 | B |

**刻意不做**（写了也白写，只会被绕过）：

- **零引用 / 死代码扫描** —— 零引用 ≠ 该删（`core/scene_tools.py` 就是反例）。
- **全库"测试数"正则扫描** —— 误报率高到没用：里程碑历史数字本就不该报警。归入 G5，
  只校验文档里标记过的位置。

## 8. 检索打分的重要教训

`core/memory.py` 的关键词路径**不走定长哈希向量的余弦**：dim=256 时哈希碰撞噪声（"零重合"文档可得 0.16 分）
会盖过真实信号（真实命中 0.12）。关键词模式改用**精确 token 集合 + IDF 加权余弦**，零重合即 0。
语义模式（有真实 Embedding）才走向量余弦。两条路径在 `MemoryRetriever` 内按 `embedding.kind` 分流。

## 9. 其他约定

- Python 3.11+；UTC 时间戳；错误码走 `core/errors`（对应 `docs/07` §9 映射表）。
- 中文文档正文与标识符用 UTF-8；**Windows 控制台在 pytest 中可能把 UTF-8 stdout 显示为乱码，
  但逻辑不受影响**（终端编码问题，非数据问题）。
- 工具 handler 可返回 `ToolResult`（如 `ok(...)`）或普通 dict；`ToolRegistry.invoke` 对 `ToolResult`
  不再二次包装。
- **规范与技能的改动会影响每个人的 agent 行为 → 走 PR**，见 `.github/CODEOWNERS`。
