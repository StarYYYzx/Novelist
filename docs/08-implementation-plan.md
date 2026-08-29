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
│   │   ├── pipeline.py         # 工序状态机（含记忆编纂、并行批次基线）
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
│   ├── storage/            # 工作区读写、检查点、schema 校验
│   │   ├── workspace.py
│   │   ├── checkpoint.py
│   │   └── schemas/*.schema.json        # 含 schemas/memory/*.schema.json
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
│   ├── chronicler.md       # 记忆编纂员
│   ├── actor.template.md   # 角色演员提示词模板（{character}/{history}/{scene} 占位）
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
- **记忆子系统初版**：工作区 `memory/` 读写 + 简单检索（关键词优先）+ 章节收尾编纂雏形（ADR-011/013）。

### M3 — 治理与交付（2 周）
- danger 级门禁流程完整（CLI 审批）。
- CLI 全命令可用；`novelist export` 发布包 + 统计。
- 敏感词过滤 + 审计日志完整。
- HTTP 服务（只读 + 审批为主）。
- **记忆子系统完整**：Embedding + 语义检索（降级到关键词）、冲突双检、RAG 增量索引。
- **角色演员**：actor 提示词模板 + takes 工具 + 多角"排演→整合"流程（ADR-012）。
- **受控围读会**：scene 场景总线 + `join/say/leave` 工具 + 结束判据收敛（ADR-014）。

### M4 — 硬化与评测（持续）
- 完整评测集（见 09）与回归，含"记忆自洽 / 人设保真"专项（A7/A8）。
- 多个 Provider 实测（云 + 本地 Ollama/vLLM），含 Embedding 能力矩阵。
- 长文（≥20 章）全流程压力测试与一致性/记忆检索命中统计。
- **并行批次衔接**：基线固化、整批一致性门禁、批内转向回流（NFR-13/A9）。
- **审核拦截降级**：MODERATION_BLOCKED 识别 + 改写/切换/人工链 + 敏感词预检（ADR-015/A10）。
- 稳定性/降级路径覆盖（含无 Embedding 时检索降级、审核拦截恢复）。

> 里程碑以"可演示的纵向切片"为单位（每次都有可跑通的新能力），而非单纯横向铺层。

## 4. 风险清单与缓解

| # | 风险 | 等级 | 缓解 |
| --- | --- | --- | --- |
| R1 | 主 Agent 上下文缓慢膨胀 | 高 | 04§5.3 压缩 + 引用式建模 + 记忆检索摘要化，先行落地 |
| R2 | 结构化输出不达标 / 模型漂移 | 高 | ADR-006 重试+降级 + 契约校验 + 检查员兜底 |
| R3 | 一致性门禁误杀（过度告警拖慢产出） | 中 | 规则分层 block/warn、可配置阈值、人工复核路径 |
| R4 | 批量并行引发设定覆盖/脏写 | 中 | 沙箱 + 独立草稿文件 + 原子写 + 锁；仅审后转正 + 批次基线 |
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
4. 再 `tools` 创作工具 + `pipeline`（含批次基线）——打通一条完整工序。
5. 再 `memory`（读取→编纂→检索）——打通"先忆 / 后纂"，回填正文写作流程。
6. 再 `actor`（takes + 排演整合）→ `scene`（围读会 + 收敛判据）——打通人设生动化。
7. 再 `moderation`（预检 + 拦截降级链）——打通内容合规。
8. 最后 `consistency` 全量、HTTP、评测（含记忆/演员/围读/审核维度）。

## 6. 后续可迁移点（Backlog 对接）
- worker 进程池化（分布式）、多项目空间、文风学习、内容回流、跨书记忆迁移（对应 02§6）。
- 工程实现细化：`core/memory.py`（记忆子系统 + RAG 索引）、`agents/*.prompt`（含角色演员提示词）、`schemas/memory/*.schema.json` 的落地节奏见 M2/M3 的评测部分与本文 §3 路线节点的对应。

> 质量、评测、一致性保障的完整内容见独立文档 `docs/09-quality-assurance.md`。
