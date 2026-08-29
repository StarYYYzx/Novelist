# Novelist — 多 Agent 长篇小说撰写系统

面向**长连载网文 / 商业化长篇**的多 Agent 写作系统。以 Claude Code 式的**单 Agent 主循环 + 按需子代理 + 工具调用**为内核，以**小说工作区（文件系统）**为共享黑板，以**可插拔 LLM 适配器**对接任意大模型后端；并内置**记忆子系统（Memory + RAG）**解决长期连载中"AI 遗忘剧情/人物经历"的痛点——写作前"先忆"、创作中由**角色演员**贴合人设试演、收尾由**记忆编纂员**把剧情与人物变化沉淀为可检索记忆。

> 当前阶段：**文档先行 + 里程碑推进中**。本仓库含完整架构文档（docs/01–09）、实体 JSON Schema 契约（schemas/）与按 M0–M4 里程碑推进的实现（src/）。M0–M2 已完成，M3 已完成门禁/导出/HTTP 服务/记忆子系统完整化，角色演员与围读会待接入。进度以 [`docs/08-implementation-plan.md`](docs/08-implementation-plan.md) 为准。

## 架构选型结论（一句话）

采用 **「编排者—工作者」为主模式**（总编 Agent 统筹、专业子代理按需派生执行）+ **黑板模式落地为小说工作区**（共享文件系统承载设定圣经、大纲、正文、审查与记忆）+ **流水线模式管理创作工序**（大纲 → 细纲 → 正文 → 审查 → 记忆编纂，带修订回流）的**混合架构**，内核复用 Claude Code 的 Agent 循环范式（观察—思考—行动—工具调用—子代理派生），以保证长文本场景下全局一致性与工程可控性。

详见 [`docs/03-architecture-decision.md`](docs/03-architecture-decision.md) 的完整选型论证与对比；记忆/演员/编纂三类组件见 ADR-011/012/013。

## 文档导航

| 文档 | 内容 |
| --- | --- |
| [docs/01-overview.md](docs/01-overview.md) | 项目背景、目标、范围、术语表、约束与假设 |
| [docs/02-requirements.md](docs/02-requirements.md) | 用户画像、用例、功能需求（FR）、非功能需求（NFR）与验收标准 |
| [docs/03-architecture-decision.md](docs/03-architecture-decision.md) | 多 Agent 架构模式对比、选型论证与 ADR 决策记录 |
| [docs/04-architecture-design.md](docs/04-architecture-design.md) | 总体架构：分层、拓扑、C4 视图、关键机制（含记忆子系统与演员试演） |
| [docs/05-agent-design.md](docs/05-agent-design.md) | 主编剧 Agent 循环、子代理/角色演员与编纂员定义、工具集与协作协议 |
| [docs/06-data-design.md](docs/06-data-design.md) | 小说工作区目录规范（含记忆层）、数据模型与 JSON Schema、状态机 |
| [docs/07-interface-design.md](docs/07-interface-design.md) | LLM 适配器接口、工具协议、记忆检索/写入接口、事件总线、对外 API |
| [docs/08-implementation-plan.md](docs/08-implementation-plan.md) | 技术栈、代码目录结构、里程碑路线图、风险清单 |
| [docs/09-quality-assurance.md](docs/09-quality-assurance.md) | 一致性保障机制、评测集、测试策略 |

## 快速阅读路径

1. 先读 [03-architecture-decision.md](docs/03-architecture-decision.md) 了解**为什么**这样选型；
2. 再读 [04-architecture-design.md](docs/04-architecture-design.md) 掌握**整体长什么样**；
3. 需要落地时对照 [05-agent-design.md](docs/05-agent-design.md) 与 [08-implementation-plan.md](docs/08-implementation-plan.md)。

## 当前实现（src/）

文档的**契约已固化为实体 Schema**（`schemas/`），核心抽象已落成可运行的实现（`src/novelist/`）：

| 能力 | 状态 |
| --- | --- |
| Provider 适配器（OpenAI 兼容 / DeepSeek / LM-Studio / Fake） | ✅ |
| Agent 循环（工具调用 + 预算收敛，本地慢模型走直出降级） | ✅ |
| 工具注册表 + 三级门禁 + 人工审批队列（跨进程持久化） | ✅ |
| 流水线状态机 + 一致性规则引擎（R-REF / R-TL）+ 检查点 | ✅ |
| 记忆子系统（检索降级 / 冲突双检 / RAG 增量索引） | ✅ |
| CLI 全命令 + FastAPI HTTP 服务 | ✅ |
| 角色演员试演 / 受控围读会（SceneBus 已就绪，未接入编排） | ⬜ |
| 敏感词过滤 + 审核拦截降级链 | ⬜ |

```
python -m pytest tests/ -q          # 运行测试（依赖 pytest/click）
python -m novelist.cli --help       # CLI（需 src 在 pythonpath）
```

> 路线：实现按 [08-implementation-plan.md](docs/08-implementation-plan.md) 的 M0–M4 里程碑推进。
> **进度以代码和 `docs/08` 为准**——文档自述可能滞后于真实实现。
