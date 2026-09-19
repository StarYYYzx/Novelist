# Novelist — 多 Agent 长篇小说撰写系统

> 系统写初稿 · 人工改正文

一套面向**长连载网文 / 商业化长篇**的多 Agent 写作系统，以「Claude Code 式 **单 Agent 主循环 + 按需子代理 + 工具调用**」为内核、以**小说工作区（文件系统）**为共享黑板、以**可插拔 LLM 适配器**对接任意大模型后端；内置**记忆子系统（Memory + RAG）**解决长期连载中"AI 遗忘剧情 / 人物经历"的痛点——写作前"先忆"、创作中按需**角色试演**贴合人设、收尾由**记忆编纂员**把剧情与人物变化沉淀为可检索记忆。

设计文档先行（`docs/01–13`），实体交互以 **JSON Schema 契约**（`schemas/`）固化，实现按 **M0–M4 里程碑**推进（`src/novelist/`）。

---

## 核心特性

| 领域 | 能力 |
| --- | --- |
| **创作链路** | 一句话创意 / 已有稿子 → 设定圣经 + 大纲 + 细纲（Forge 构建层）→ 串行逐章正文 → 一致性审查 → 修订回流 |
| **正文生成** | 事件循环 + 续写 + 事件/章级润色 + 延迟拟题 + 首登场身份织入 + 生成预算控制 |
| **一致性保障** | 确定性规则引擎（引用完整性 / 时间线 / 境界进退 / 设定条目）+ LLM 审校双层校验 |
| **记忆子系统** | 剧情事件/人物经历/关系碎片索引 + 关键词/向量双通道检索降级 + 冲突双检 + RAG 增量索引 + LLM 侧选 rerank |
| **世界状态** | 人物硬状态 / 时间轴 / 定时事项（pending）记账 + 卷末既成事实清单跨卷连续性 |
| **明暗线** | 线索（Line）子系统：登记 / 检查点 / 卷末审计 / 到期强制项 |
| **人工反馈** | 设定集自由语意见 → 字段级定位 → 审批 → 原子写回（covenant 强制人工确认） |
| **草稿溯源** | 每章成稿自动落**源清单**（人物卡 / 线索 / 世界规则 / 记忆基线 / prompt 指纹），供修订归因 |
| **交付运维** | 三级工具门禁 + 跨进程审批队列 + 检查点快照 + 中断恢复 + 审计报告 + Markdown⇄Word 互转 |
| **接口** | 全功能 CLI + FastAPI HTTP 服务 |

## 架构选型（一句话）

采用 **「编排者—工作者」为主模式**（主编剧 Agent 统筹、专业子代理按需派生）、**黑板模式落地为小说工作区**（共享文件系统承载设定圣经 / 大纲 / 正文 / 审查 / 记忆）、**流水线模式管理创作工序**（大纲 → 细纲 → 正文 → 审查 → 记忆编纂，带修订回流）的**混合架构**，内核复用 Claude Code 的 Agent 循环范式（观察—思考—行动—工具调用），保证长文本下的全局一致性与工程可控性。完整论证见 [`docs/03-architecture-decision.md`](docs/03-architecture-decision.md)。

## 技术栈与可插拔模型

- **运行时**：Python 3.11+（实测 3.14）
- **接口层**：click（CLI）、FastAPI / uvicorn（HTTP 服务）、pydantic / jsonschema（数据契约）
- **存储**：文件系统为主 + SQLite 辅助索引（可再生缓存），全原子写
- **LLM 适配器**：**单轨工厂** `providers.create()` 统一实例化；思维模式按任务路由（生成类关思考省钱、判断类开思考提准）
- **支持的模型后端**（OpenAI 兼容协议为主，一套基类）：

  `deepseek`（默认，`deepseek-v4-flash`）· `openai` · `qwen` · `kimi` · `glm` · `anthropic` · `ollama` · `vllm` · `custom`（自定义端点）· `fake/scripted`（测试替身）

- **密钥管理**：API Key 解析优先级为 **CLI 显式参数 > 环境变量 > 本机 `.env`**（`.env` 已 gitignore，Key 永不落库；`~/.env` 形如 `DEEPSEEK_API_KEY=sk-...`）

## 快速开始

```bash
# 安装（可编辑模式）
pip install -e ".[dev,providers]"

# 跑测试（本仓库默认不带真实 LLM，快而稳）
python -m pytest tests/ -q          # 1217 passed · 1 skipped（1219 收集，slow 默认跳过）

# 查看 CLI 帮助
python -m novelist.cli --help       # 需 src 在 sys.path；或 pip install 后直接用 novelist

# 新建一本"书"
python -m novelist.cli init my_novel --title "书名"

# 用 forge 一键建书：一句话创意 → 圣经 + 大纲 + 细纲
python -m novelist.cli forge seed "废柴主角觉醒上古血脉，一路逆袭" --provider deepseek
```

常见命令：

| 命令 | 作用 |
| --- | --- |
| `forge seed BRIEF` / `forge ingest 旧稿路径` | 从创意 / 旧稿构建设定集与大细纲 |
| `chapter DIR --vol 1 --ch 1 --provider deepseek` | 串行写一章正文（默认直出；事件循环、实时回写记忆） |
| `draft DIR [1:3] [--text]` | 汇报该章草稿生成时的源清单（草稿溯源） |
| `feedback DIR --opinion "人物性子太快了"` | 自由语设定意见 → 字段级审批写回 |
| `review DIR --provider deepseek` | 全量一致性审查（规则层 + 语义层） |
| `promote DIR --vol 1 --ch 1` | 草稿转正为正式章节（门禁审批） |
| `server --host 127.0.0.1 --port 8000` | 启动 HTTP 服务 |

> 绝大多数命令的 `--provider` 默认是 `fake`（测试替身）；**真跑必须显式 `--provider deepseek`**。完整参考见 [`docs/命令手册.md`](docs/命令手册.md)。

## 仓库结构

```
docs/            01–13 设计文档 + 命令手册 + 里程碑进度（docs/08）
schemas/         JSON Schema 契约（bible / outline / memory …）
src/novelist/
  core/          Agent 循环、记忆、写回、世界状态、事件循环、一致性、审批、草稿溯源、设定反馈…
  forge/         构建层（一句话/旧稿 → 圣经 + 大纲 + 细纲）
  providers/     单轨工厂 + 各模型预设 + 密钥管理
  storage/       工作区沙箱、检查点、SQLite 辅助索引
  tools/         工具注册表（filesys / writing / memory / governance）
  consistency/   确定性规则引擎与审校
  cli.py / server.py / config.py
tests/           1219 收集（1217 passed · 1 skipped · 1 deselected）；单测绝不真调 LLM（docs/09）
```

## 文档导航

| 文档 | 内容 |
| --- | --- |
| [docs/01-overview.md](docs/01-overview.md) | 背景、目标、范围、术语表、约束与假设 |
| [docs/02-requirements.md](docs/02-requirements.md) | 用户画像、用例、功能需求（FR / NFR）与验收标准 |
| [docs/03-architecture-decision.md](docs/03-architecture-decision.md) | 多 Agent 模式对比、选型论证与 ADR-001…036 决策记录 |
| [docs/04-architecture-design.md](docs/04-architecture-design.md) | 总体架构：分层、拓扑、C4 视图、关键机制 |
| [docs/05-agent-design.md](docs/05-agent-design.md) | 主编剧循环、子代理 / 角色演员 / 编纂员、工具协作协议 |
| [docs/06-data-design.md](docs/06-data-design.md) | 工作区目录规范（含记忆层）、数据模型与 JSON Schema |
| [docs/07-interface-design.md](docs/07-interface-design.md) | LLM 适配器、工具协议、记忆检索/写入、对 API |
| [docs/08-implementation-plan.md](docs/08-implementation-plan.md) | 技术栈、代码结构、**里程碑路线图（M0–M4）与进度** |
| [docs/09-quality-assurance.md](docs/09-quality-assurance.md) | 一致性保障、评测集、测试策略 |
| [docs/10-forge.md](docs/10-forge.md) | 构建层（一句话/旧稿 → 圣经+大纲+细纲） |
| [docs/11-coding-standard.md](docs/11-coding-standard.md) | 编码规范（分层 / 命名 / 类型 / 错误 / 测试） |
| [docs/命令手册.md](docs/命令手册.md) | 顶层命令与 forge 子命令速查 |

## 当前实现与里程碑

- **M0 骨架与契约** ✅ **M1 Agent 循环闭环** ✅ **M2 流水线与一致性** ✅
- **M3 治理与交付**（大面积完成）：门禁/HTTP/记忆完整化/质量增强/事件循环/世界状态/注册表/明暗线/RAG **完成**；**Forge 构建层**、**人物一致性四件套**（ADR-020）、**P0 闸门**、**ADRs 021–029**（广播选角 / 角色工厂 / 数据补喂 / 线索 / 商讨扩展 / 事件实然回写 / 承诺账本 / rerank / 任务持久化）**已落地**；**M3z 人工反馈通道批次A 完成**（设定集自由语 → 审批写回）、**批次B 草稿溯源完成**（ADR-030）
- **M4 硬化与评测**（持续）：完整评测集、多 Provider 实测、长文全流程压力测试

能力速览：

| 能力 | 状态 |
| --- | --- |
| Provider 单轨工厂 + 主流模型预设 + 自定义端点 | ✅ |
| 事件循环 / 续写 / 润色 / 延迟拟题 / 生成预算 | ✅ |
| 记忆子系统（RAG 索引 + 双通道检索 + 冲突双检 + rerank） | ✅ |
| 一致性规则引擎 + LLM 审校双层校验 | ✅ |
| 三级工具门禁 + 审批队列 + 检查点 + HTTP 服务 | ✅ |
| Forge 构建层（创意/旧稿 → 圣经+大纲+细纲） | ✅ |
| 设定集人工反馈通道（ADR-029 批次A） | ✅ |
| 草稿溯源源清单（ADR-030 批次B） | ✅ |
| 角色演员试演 / 受控围读会（SceneBus 已就绪，编排未接入） | ⬜ 待接入 |
| 人工修订识别 + 归因回写（ADR-031 批次C） | ⬜ 规划中 |

## 测试与一致性

```bash
python -m pytest tests/ -q        # 全量（默认跳过慢测试）
python -m pytest -m slow          # 显式跑真实 LLM / 慢集成
```

测试纪律：**单元测试绝不真调 LLM**（docs/09 §2.1），用 `fake/scripted` 替身；一致性优先（无人物/情节冲突）高于艺术质量，接受多次 LLM 调用换更高产出质量。

---

> 说明：项目为设计先行，并发推进实现。**进度以代码与 [`docs/08-implementation-plan.md`](docs/08-implementation-plan.md) 为准**，本 README 的阶段自述可能滞后于真实实现——判断做到哪一步，看 `git log` 与测试，而非文档自述。