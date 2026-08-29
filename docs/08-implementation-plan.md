# 08 · 工程实现规划

> 怎么把前面的设计落地。技术栈、代码结构、里程碑路线图、风险清单。**本文是"何时做、怎么做"，答案已是设计产物。**

## 1. 技术栈选择

| 层 | 选择 | 理由 |
| --- | --- | --- |
| 语言 | Python 3.11+（当前环境 3.14 亦兼容） | Agent/AI 生态成熟、express 循环易写 |
| 异步 | asyncio + 进程池 | 单进程内并发子代理/批量章节 |
| 类型/校验 | Pydantic v2 | JSON Schema 契约直接映射校验 |
| Agent 循环 | **自研轻量内核**（不绑死框架） | 设计独特、需深度定制；可隔离依赖 |
| LLM 接口 | 自研 Provider 抽象 + 各 SDK 可选依赖 | ADR-003 插件化 |
| 本地推理 | Ollama / vLLM（OpenAI 协议） | 满足本地部署需求 |
| 配置 | TOML + 环境变量 | 简单、版本可控 |
| 存储 | 文件系统工作区 + 轻量 JSON | ADR-004/010，兼容 Git |
| 日志 | structlog / logging | 事件化、结构化 |
| 测试 | pytest + pytest-asyncio | 标准 |
| 对外 | click(CLI) + FastAPI(HTTP) | 轻量、成熟 |

> **不引入**重量级多 Agent 框架作为内核依赖（决定见 03§6），仅可选用其思想；避免抽象过度与锁定。

## 2. 代码目录结构

```
novelist/
├── pyproject.toml
├── src/novelist/
│   ├── __init__.py
│   ├── core/               # 与表层无关的内核
│   │   ├── agent_runner.py     # Agent 循环执行器（主编剧 & 子代理共用）
│   │   ├── subagent.py         # 派发/回收/隔离会话管理
│   │   ├── orchestrator.py     # 主编剧具体实现（守则 + 决策）
│   │   ├── pipeline.py         # 工序状态机
│   │   ├── permission.py       # 门禁与策略
│   │   ├── budget.py           # token/成本预算
│   │   └── events.py           # 事件总线/审计
│   ├── tools/              # 工具注册表 + 各工具实现
│   │   ├── registry.py
│   │   ├── filesys.py      # read_file/write_file/list_dir/grep
│   │   ├── writing.py      # write_draft/promote_draft
│   │   ├── setting.py      # update_entity/add_plot_thread/set_timeline
│   │   ├── outline.py
│   │   ├── consistency.py  # run_rule_check/run_semantic_check
│   │   └── governance.py   # delete/batch_rewrite/checkpoint/publish
│   ├── providers/          # LLM Provider 适配器(插件)
│   │   ├── base.py         # LLMProvider 抽象
│   │   ├── registry.py
│   │   ├── openai.py
│   │   ├── deepseek.py
│   │   ├── anthropic.py
│   │   ├── ollama.py
│   │   └── vllm.py
│   ├── consistency/        # 一致性规则引擎 + 语义检
│   │   ├── rules.py        # 确定性规则
│   │   └── semantic.py
│   ├── storage/            # 工作区读写、检查点、schema 校验
│   │   ├── workspace.py
│   │   ├── checkpoint.py
│   │   └── schemas/*.schema.json
│   ├── cli.py              # click 命令行
│   ├── server.py           # FastAPI HTTP
│   └── config.py
├── agents/                 # 各 Agent 系统提示（提示词独立成文本文件）
│   ├── orchestrator.md
│   ├── worldbuilder.md
│   ├── outliner.md
│   ├── wordsmith.md
│   ├── reviewer.md
│   ├── plotkeeper.md
│   └── inspector.md
├── schemas/                # 复制的公开 JSON Schema（供外部校验）
├── tests/
└── docs/
```

## 3. 里程碑路线图

### M0 — 骨架与契约（1 周）
- 建立仓库、pyproject、CI 骨架。
- 落地 `core/agent_runner.py` 最小循环 + `providers/base.py` 抽象 + 事件总线。
- 落定 `storage/schemas/*` 初版 + 工作区目录解析器。
- 冒烟用例：一次 `complete()` + 一次工具调用。

### M1 — Agent 循环闭环（2 周）
- 主编剧可按剧本自主调用**只读 + 写作**工具完成"生成一章草稿"循环。
- 工具注册表 + 门禁（safe/sensitive 先行）接入。
- 子代理派发/回收可用（至少 1 个：文字匠）。
- 检查点初版（快照 + 恢复）。

### M2 — 流水线与一致性（2 周）
- 工序状态机完整贯通（大纲→细纲→正文→审查→修订）。
- 一致性规则引擎（引用完整性、时间线）落地。
- 审校师（LLM 语义检）接入并合并告警。
- 并行批量生成（2–4 章）+ 预算控制。

### M3 — 治理与交付（2 周）
- danger 级门禁流程完整（CLI 审批）。
- CLI 全命令可用；`novelist export` 发布包 + 统计。
- 敏感词过滤 + 审计日志完整。
- HTTP 服务（只读 + 审批为主）。

### M4 — 硬化与评测（持续）
- 完整评测集（见 09）与回归。
- 多个 Provider 实测（云 + 本地 Ollama/vLLM）。
- 长文（≥20 章）全流程压力测试与一致性统计。
- 稳定性/降级路径覆盖。

> 里程碑以"可演示的纵向切片"为单位（每次都有可跑通的新能力），而非单纯横向铺层。

## 4. 风险清单与缓解

| # | 风险 | 等级 | 缓解 |
| --- | --- | --- | --- |
| R1 | 主 Agent 上下文缓慢膨胀 | 高 | 04§5.3 压缩 + 引用式建模，先行落地 |
| R2 | 结构化输出不达标 / 模型漂移 | 高 | ADR-006 重试+降级 + 契约校验 + 检查员兜底 |
| R3 | 一致性门禁误杀（过度告警拖慢产出） | 中 | 规则分层 block/warn、可配置阈值、人工复核路径 |
| R4 | 批量并行引发设定覆盖/脏写 | 中 | 沙箱 + 独立草稿文件 + 原子写 + 锁；仅审后转正 |
| R5 | 成本失控（token 爆炸） | 中 | 预算上限阻断 + 并发节流 + 上下文压缩 |
| R6 | Provider 能力矩阵差异（降级差异大） | 中 | capabilities 探测 + 降级总表（07§7）+ 适配器白盒测试 |
| R7 | 提示词与实现耦合、难维护 | 中 | 提示词独立成 `agents/*.md` + 版本管理 + 评测驱动 |
| R8 | 沙箱/路径安全漏洞 | 高 | 严格白名单解析、路径穿越测试、禁止绝对路径越界 |

## 5. 实现顺序建议（TDD 取向）
1. 先 `storage`（工作区/schema/检查点）——是所有上层的地基。
2. 再 `providers`（抽象 + 1 个真适配器）——打通 LLM。
3. 再 `core/agent_runner + tools/registry + permission`——打通 Agent 循环。
4. 再 `tools` 创作工具 + `pipeline`——打通一条完整工序。
5. 最后 `consistency`、HTTP、评测。

## 6. 后续可迁移点（Backlog 对接）
- worker 进程池化（分布式）、多项目空间、文风学习、内容回流（对应 02§6）。

# 09 · 质量保障与评测

## 1. 质量目标（对应 NFR）
- 一致性规则层零失败（NFR-4 基线）。
- Provider 可插拔不受核心改动影响（NFR-2）。
- 每次变更可回归（NFR-7）。
- 结果质量可用离线评测集量化（NFR-10）。

## 2. 测试策略

| 层 | 方式 | 工具 |
| --- | --- | --- |
| 单元 | Provider 抽象（用 mock/fake completion）、工具、规则引擎、状态机 | pytest |
| 集成 | 工作区读写→工具→一致性全链路 | pytest + tmp 工作区 |
| 契约 | schema 校验、事件日志 schema | jsonschema / pydantic |
| E2E | 跑通"创意→1章"、及"20 章一致性" | CI 长任务 |
| 沙箱安全 | 路径穿越、越界读写的负面用例 | 专项 |

### 2.1 LLM 相关测试黄金法则
- 单元测试**绝不真正调 LLM**，用 FakeProvider 注入（确定性）。
- 真实模型调用进**集成/评测**，受 budget 控制、可标记 skip。
- 适配器测试覆盖 `capabilities` 差异导致的降级路径。

## 3. 评测集与评分（NFR-10）

评测集 = 一组"已知正确/应改"的样例项目（含设定圣经 + 大纲 + 若干章），供自动跑分。

| 维度 | 说明 | 指标 |
| --- | --- | --- |
| 设定一致性 | 人物/地点/力量/时间线不矛盾 | 引用完整率、矛盾数 |
| 文风保持 | 叙述与角色语气符合 style.json | 抽样人工 + 引入易检信号 |
| 情节逻辑 | 事件因果、伏笔回收合理 | 抽样人工评分 |
| 结构化输出 | 契约解析成功率 | json parse/validate 通过率 |
| 门禁正确 | 危险操作均可被正确拦截/放行 | 用例通过率 |
| 成本效率 | 每章 token 用量 | 实测统计 |

评分在执行 `novelist eval <evalset>` 后输出报告（落 `reports/stats/`）。

## 4. 一致性保障的持续质检（上线后）
- 每章转正前置 `run_rule_check`（block 级阻断）。
- `run_semantic_check` 结果进入审查工单驱动修订。
- 统计化监控：设定实体新增/矛盾趋势、伏笔逾期数量，供人工关注。

## 5. 人工评价与审计
- 阶段性抽样人工评审（release 门禁）。
- 基于事件日志的完整审计重放可用于定位质量问题根因。

## 6. 验收回归对照
验收标准 A1–A6（02§5）分别映射到 M2/M3/M4 的 E2E：A1/A2→M2 一致性封闭；A3→M4 多 Provider E2E；A4→检查点 kill-test；A5→门禁用例；A6→导出+过滤测试。
