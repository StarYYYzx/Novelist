# Novelist — 多 Agent 长篇小说撰写系统

面向**长连载网文 / 商业化长篇**的多 Agent 写作系统。以 Claude Code 式的**单 Agent 主循环 + 按需子代理 + 工具调用**为内核，以**小说工作区（文件系统）**为长期记忆与共享黑板，以**可插拔 LLM 适配器**对接任意大模型后端。

> 当前阶段：**软件工程设计（文档先行）**。本仓库为完整的架构与设计文档集，暂不含实现代码。

## 架构选型结论（一句话）

采用 **「编排者—工作者」为主模式**（总编 Agent 统筹、专业子代理按需派生执行）+ **黑板模式落地为小说工作区**（共享文件系统承载设定圣经、大纲、正文与审查记录）+ **流水线模式管理创作工序**（大纲 → 细纲 → 正文 → 审查 → 修订，带反馈回流）的**混合架构**，内核复用 Claude Code 的 Agent 循环范式（观察—思考—行动—工具调用—子代理派生），以保证长文本场景下全局一致性与工程可控性。

详见 [`docs/03-architecture-decision.md`](docs/03-architecture-decision.md) 的完整选型论证与对比。

## 文档导航

| 文档 | 内容 |
| --- | --- |
| [docs/01-overview.md](docs/01-overview.md) | 项目背景、目标、范围、术语表、约束与假设 |
| [docs/02-requirements.md](docs/02-requirements.md) | 用户画像、用例、功能需求（FR）、非功能需求（NFR）与验收标准 |
| [docs/03-architecture-decision.md](docs/03-architecture-decision.md) | 多 Agent 架构模式对比、选型论证与 ADR 决策记录 |
| [docs/04-architecture-design.md](docs/04-architecture-design.md) | 总体架构：分层、拓扑、C4 视图、关键机制 |
| [docs/05-agent-design.md](docs/05-agent-design.md) | 主编剧 Agent 循环、子代理定义、工具集与协作协议 |
| [docs/06-data-design.md](docs/06-data-design.md) | 小说工作区目录规范、数据模型与 JSON Schema |
| [docs/07-interface-design.md](docs/07-interface-design.md) | LLM 适配器接口、工具协议、事件总线、对外 API |
| [docs/08-implementation-plan.md](docs/08-implementation-plan.md) | 技术栈、代码目录结构、里程碑路线图、风险清单 |
| [docs/09-quality-assurance.md](docs/09-quality-assurance.md) | 一致性保障机制、评测集、测试策略 |

## 快速阅读路径

1. 先读 [03-architecture-decision.md](docs/03-architecture-decision.md) 了解**为什么**这样选型；
2. 再读 [04-architecture-design.md](docs/04-architecture-design.md) 掌握**整体长什么样**；
3. 需要落地时对照 [05-agent-design.md](docs/05-agent-design.md) 与 [08-implementation-plan.md](docs/08-implementation-plan.md)。
