# AGENTS.md — Novelist 仓库指南

Novelist 是多 Agent 长篇小说撰写系统。设计文档先行，当前处于**文档 + 代码脚手架**阶段。

## 文档层（docs/01–09）

- `01` 概述 + "以写代码的方式写小说"（Claude Code 类比）| `02` 需求（FR/NFR/UC/验收 A1–A10）| `03` 架构选型 + ADR（-001..-016）| `04` 总体架构 | `05` Agent 设计（主编剧/子代理/演员/编纂）| `06` 数据设计（工作区+SQLite 索引）| `07` 接口 | `08` 实现规划（M0–M4/风险）| `09` 质量。
- 关键决策以 **ADR** 记录编辑在 `docs/03`，新增机制须回填对应 ADR 与 FR/验收。

## 契约层（schemas/ 与 src 桩）

- `schemas/` 是持久事实源的正式 JSON Schema（bible/outline/memory/config/policy/project/events/llm）。**改动 schema 须同步审查 docs/06 §5.2 的规则引擎白名单**。
- `src/novelist/` 是契约接口桩：`core/{llm,tools,events,session,errors,memory,scene,scene_tools,pipeline,moderation,agent_runner}`、`providers/{__init__,base,fake}`、`storage/{workspace,indexdb}`、`cli.py`、`config.py`。

## 命令

```
python -m pytest tests/ -q        # 冒烟测试（tests/，覆盖核心桩；pythonpath=src 在 pyproject 配置）
python -m novelist.cli --help     # CLI 骨架（保证 src 在 sys.path）
```

- 依赖：pytest、click 等（见 pyproject）。未做 editable install；pytest 通过 `[tool.pytest.ini_options].pythonpath=["src"]` 注入。
- 新增 Provider 须实现 `core/llm.LLMProvider` Protocol + `providers/` 注册；测试用 `providers/fake.py`。

## 约定

- Python 3.11+；UTC 时间戳；错误码走 `core/errors`（对应 docs/07 §9 映射表）。
- 中文文档正文与标识符用 UTF-8；**Windows 控制台在 pytest 中可能把 UTF-8 stdout 显示为乱码，但逻辑不受影响**（是终端编码问题，非数据问题）。
- 围读会 `SceneBus` 用 `threading.Lock`（不可重入）——不要在持锁时再调用加锁方法，避免死锁。
- 工作区数据（`novel_workspace/`、`*.index.db`）不入 git（见 .gitignore）。
