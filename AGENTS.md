# AGENTS.md — Novelist 仓库指南

Novelist 是多 Agent 长篇小说撰写系统。设计文档先行，当前处于**按 M0–M4 里程碑推进实现**阶段（M0–M2 已完成，M3 进行中）。

> **进度以代码和 `docs/08-implementation-plan.md` 为准。** README/本文档的阶段自述可能滞后于真实实现——
> 判断实现到哪一步，看 `git log` 和测试，不要只看文档自述。

## 文档层（docs/01–09）

- `01` 概述 + "以写代码的方式写小说"（Claude Code 类比）| `02` 需求（FR/NFR/UC/验收 A1–A10）| `03` 架构选型 + ADR（-001..-016）| `04` 总体架构 | `05` Agent 设计（主编剧/子代理/演员/编纂）| `06` 数据设计（工作区+SQLite 索引）| `07` 接口 | `08` 实现规划（M0–M4/风险，**含各里程碑完成状态**）| `09` 质量。
- 关键决策以 **ADR** 记录编辑在 `docs/03`，新增机制须回填对应 ADR 与 FR/验收。
- `docs/人工审查.md` 是用户的审查意见与待办（含空白的"第二批"，等用户填）。

## 代码层（src/novelist/）

- `core/`：内核。`llm`（Provider 抽象）、`embedding`（Embedding + 关键词降级）、`memory`（记忆索引/检索/写入）、
  `writeback`（事件实时回写）、`tools`（注册表 + 三级门禁）、`approval`（审批队列）、`pipeline`（工序状态机）、
  `orchestrator` + `agent_runner`（Agent 循环）、`scene`（围读会总线）、`export`、`errors`、`events`、`moderation`。
- `providers/`：`openai`（OpenAI 兼容，含审核拦截识别）、`deepseek`、`lmstudio`、`fake`（测试替身）。
- `storage/`：`workspace`（沙箱 + 原子写）、`checkpoint`（双轨快照）、`indexdb`（SQLite 辅助索引）、`models`（Schema 校验）。
- `tools/`：`filesys` / `writing` / `memory_tools` / `governance`，经 `build_registry()` 装配。
- `cli.py`（click）、`server.py`（FastAPI）、`config.py`（TOML 配置）。

## 命令

```
C:/Python314/python.exe -m pytest tests/ -q    # 全量测试（Windows 本机；见下方"运行环境"）
python -m novelist.cli --help                  # CLI（保证 src 在 sys.path）
```

- 依赖：pytest、click 等（见 pyproject）。未做 editable install；pytest 通过 `[tool.pytest.ini_options].pythonpath=["src"]` 注入。
- 默认 `addopts = -m 'not slow'`，本地 LM-Studio / 真实 API 的慢测试需 `pytest -m slow` 显式运行。
- 新增 Provider 须实现 `core/llm.LLMProvider` Protocol + 在 `providers/__init__.py` 注册；单元测试用 `providers/fake.py`，
  **绝不真调 LLM**（docs/09 §2.1）。

### 运行环境（Windows 本机）

- 托管 Python 3.13.12 **未安装 pytest**；用系统解释器 `C:/Python314/python.exe`（pytest 9.1.1）跑测试。

## 约定

- Python 3.11+；UTC 时间戳；错误码走 `core/errors`（对应 docs/07 §9 映射表）。
- 中文文档正文与标识符用 UTF-8；**Windows 控制台在 pytest 中可能把 UTF-8 stdout 显示为乱码，但逻辑不受影响**（是终端编码问题，非数据问题）。
- 围读会 `SceneBus` 用 `threading.Lock`（**不可重入**）——不要在持锁时再调用加锁方法，避免死锁。
  `ApprovalQueue.wait_for_decision` 的超时分支即为此在锁内直接标记 deny 而不重入 `decide()`。
- 工作区数据（`novel_workspace/`、`*.index.db`、`demo/`）不入 git（见 .gitignore）。测试一律用 `tmp_path`。
- **改 schema 须同步审查 docs/06 §5.2 的规则引擎白名单**，避免引擎不认识新字段。
- 工具 handler 可返回 `ToolResult`（如 `ok(...)`）或普通 dict；`ToolRegistry.invoke` 对 `ToolResult` 不再二次包装。

## 检索打分的重要教训

`core/memory.py` 的关键词路径**不走定长哈希向量的余弦**：dim=256 时哈希碰撞噪声（"零重合"文档可得 0.16 分）
会盖过真实信号（真实命中 0.12）。关键词模式改用**精确 token 集合 + IDF 加权余弦**，零重合即 0。
语义模式（有真实 Embedding）才走向量余弦。两条路径在 `MemoryRetriever` 内按 `embedding.kind` 分流。
