# 13 — AI 协作接手指南（开发要点 · 2026-09-06 定稿）

> 本文档面向**后续接手的人类开发者及其 AI 助手**。目标：30 分钟内可跑通、可改码、知道接下来做什么。
> 基线：提交 `ca53513`（2026-09-11），全量回归 **966 passed / 2 skipped**。
> 项目节奏极快，本文档会滞后——**进度永远以代码 + `git log` + `docs/08-implementation-plan.md` 为准**。

---

## 0. 30 秒速览

- **项目**：Novelist——多 Agent 长篇小说创作系统。把 Claude Code 的工程化范式（bible/context/子代理/工具门禁）
  映射到网文创作：一句话或已有稿件 → bible + 大纲 + 细纲 → 串行逐章正文 + 事件级实时回写。
- **技术栈**：Python 3.11+，pytest，零重依赖（docx 互转用 zipfile+xml 手写）。数据即文件（JSON/MD），
  SQLite(`.index.db`) 与 RAG 向量**仅是可再生缓存**。
- **测试域**：男频修仙网文（≥20 角色 / ≥10 事件 / 多卷长篇）。
- **模型纪律（2026-09-06 拍板 + 09-07 扩展）**：
  1. **云端服务器（AutoDL llama-server）与本地 LM-Studio 均已停用，相关代码已删除**；
  2. **默认生成走 DeepSeek API `deepseek-v4-flash`**（`providers/deepseek.py`，注释明示"禁用 pro"）；
     2026-09-07 起 Provider 单轨工厂（`providers.create`）支持多厂商切换——
     `openai/qwen/kimi/glm/anthropic/ollama/vllm/custom`（P0-2 闭环）；
  3. **embedding 走本地 fastembed ONNX**（`LocalEmbedding`，nomic-embed-text-v1.5，768 维，CPU 零 torch）；
     模型不可用时由 `make_embedding` 降级关键词路径（token 集合 + IDF 余弦）。首次调用需下载模型
     （走 hf-mirror），**缓存坑见 §8**。

## 1. 模型接入（改任何生成代码前必读）

| 用途 | 通道 | 说明 |
| --- | --- | --- |
| 生成调用（默认） | DeepSeek API，`deepseek-v4-flash` | `providers/deepseek.py`。注释明示"禁用 pro" |
| 生成调用（可切换，2026-09-07 起） | **Provider 单轨工厂** `providers.create(name, **kw)`（P0-2 已闭环） | 命名预设：`deepseek / openai / qwen / kimi / glm / anthropic / ollama / vllm` + `custom`（任意 OpenAI 兼容端点，需显式 `--api-base`）；`--provider` 传名即可，`--api-base/--api-key/--model` 可覆盖预设 |
| Key 管理 | `providers/secrets.py` + `.env`（gitignored） | Key 按优先级查环境变量 → `.env` 文件 → 显式参数；遮蔽/脱敏。**Key 永不落配置文件/工作区/蓝图** |
| Embedding | **本地 fastembed ONNX**（`LocalEmbedding`，nomic-embed-text-v1.5，768 维，CPU 零 torch） | `core/embedding.py`：`make_embedding("auto")` 内置模型优先 → openai（有 key）→ 关键词降级。首次调用下载模型到 `%TEMP%/fastembed_cache`（⚠️ 见总账 P-FE：缓存落 Temp 易被清理；离线环境需预置缓存） |
| 单元测试 | `providers/fake.py` / ScriptedProvider | **铁律：单测绝不真调 LLM**（docs/09 §2.1） |

- 思考模式：DeepSeek v4 支持按请求思考开关；思考 token 计入 max_tokens 的教训依然适用——
  content 空且 finish=length ≠ "没话说"。已有 `reasoning_aware=True` 自动加预算重试与
  broadcast 重试循环（异常/blocked/空 content/空 cast）兜底，不要删。
- 生成预算经验值：正文 6000 token 起；云端同款 ctx 16384 的预算夹击问题（思考吃光 vs OOM）随云端停用基本消解。

## 1.5 命令手册

全部 CLI/forge/docx 命令、参数与全链路示例：**`docs/命令手册.md`**。

## 2. 运行环境（Windows 本机）

```bash
# 全量回归（唯一权威验收）。两个环境变量是必须前置——否则 safe-delete 误杀测试自身的 unlink
CODEBUDDY_SAFE_DELETE_ENABLED=0 CODEBUDDY_SAFE_DELETE_BULK_THRESHOLD=100000 \
  C:/Python314/python.exe -m pytest tests/ -q --tb=short
# stdout 被日志淹没时：> /tmp/pt.txt 2>&1 后 grep passed/failed
```

- 解释器：**必须 `C:/Python314/python.exe`**（pytest 9.1.1）。托管 Python 3.13.12 无 pytest；
  anaconda `/e/python/ana/python` 无 fastapi 跑不了全量，别用。
- 出现 ModuleNotFoundError 的应急修复（直接重装到系统解释器）：
  `C:/Python314/python.exe -m pip install pydantic click "fastapi>=0.110" "uvicorn[standard]>=0.29" "jsonschema>=4.20" structlog pytest pytest-asyncio python-docx openai httpx`
- Windows 控制台 UTF-8 显示乱码是**终端编码问题**，逻辑与数据不受影响。
- 全量基线约 75-90s；默认 `addopts = -m 'not slow'`，真机慢测试须 `pytest -m slow` 显式跑。

## 3. 仓库地图与文档导读

```
src/novelist/
  core/       内核：orchestrator(编排+事件循环) / memory(检索) / worldstate(硬状态+时间轴)
              chronicler(事件抽取+回写) / chronicler_agent(证据仲裁环 ADR-032) / lines(线索账本 ADR-025)
              broadcast(扬名播报) / director(人物调度) / tasks(任务板 ADR-028) / polish
              bible_feedback(设定人工反馈 ADR-029) / draft_provenance(草稿溯源 ADR-030)
              embedding(fastembed 本地 ONNX / 关键词降级) / consistency(规则引擎)
  consistency/ reviewer(审校) / reviewer_agent(审校 Agent 化 ADR-032) / rules(确定性规则)
  forge/      构建层：seed/ingest(两种建书模式) engine+nodes(递归蓝图) ask(商讨轮)
              shell(常驻会话壳 REPL) console(项目交互中枢`novelist console`) covenant(承诺账本 ADR-026)
              coherence/review(蓝图审查+人工闸门 ADR-024)
  providers/  单轨工厂 `providers.create`（见 docs/07 §2.4）：deepseek(默认生成) /
              openai(兼容基类) / qwen/kimi/glm/anthropic/ollama/vllm(预设) / custom / fake(测试) / secrets(.env+key)
  storage/    workspace(沙箱+原子写) checkpoint / indexdb
docs/01-13   设计文档（01 概述 / 02 需求FR / 03 ADR / 06 数据契约 / 08 实现规划+里程碑状态
             / 10 Forge / 11 编码规范★ / 12 流程走查 / 13 本指南）
docs/命令手册.md               CLI/forge/docx 全命令参数与示例
docs/问题总账-2026-09-03.md   已知问题总账（F1-F8/B1 等，改码前先查有没有人踩过）
novel_workspace/<pid>/        项目数据（不入 git）；_harness/ 下是探针脚本（入库）
tests/                        87 个测试文件；测试名 m19/m21-26/m3z/m3aa = 里程碑编号
```

- **写代码前先读 docs/11**（分层依赖/函数上限/类型标注/异常继承 NovelistError/Provider 单轨注册）。
- 改 schema 须同步 docs/06 §5.2 规则引擎白名单。
- README/AGENTS.md 的阶段自述可能滞后于真实进度——**判断进度看 git log 和测试数，不看文档自述**。

## 4. 架构五条铁律（违反即制造一致性事故）

1. **串行逐章、事件级回写（ADR-002/013）**：正文严格串行；事件落定**立即**回写（不等章末）。
   回写 7 类目标全确定性零 LLM（chronicler.commit）：plot_events / 角色经历 / 角色视角 / worldstate
   / 线索账本 / plot_threads / 时间轴。每事件 4 次 LLM（主生成+polish+抽取+视角）是拍板过的
   "调用数换质量"，不要"优化"掉。
2. **文件=持久事实源（ADR-016）**：bible/memory/lines/worldstate 全是 JSON/MD；SQLite 与 RAG
   向量删了必须能重建。别往数据库里搬真相。
3. **bible=应然（人设该怎样）/ memory=实然（实际发生）分离互引（ADR-011）**；角色卡实然字段
   （power.level）由 worldstate 合法推进才同步，倒退/越级不动卡；R-STATE 单调性锚在 worldstate
   `baselines`（rules.py 优先读它）。
4. **线索账本一份账（ADR-025）**：章纲 `lines_present` 是唯一决策层；模型只有提名权
   （pending→人工转正）；计划外闭合=closing_candidate 人工确认；微线不入账。
5. **新开关一律 opt-in 默认关**（seam_review/volume_facts/coherence_review/microbeat/seam_state/
   mem-rerank 全部如此）：默认关闭=行为完全不变，真机验收由 harness 显式开启。
   承诺账本（ADR-026）把"走向"钉死：细纲可滚动修订，但触碰已承诺条目必须转 ADR-024 人工闸门。

## 5. 约定俗成的协作流程（历次迭代沉淀，勿跳步）

1. **先讨论后动码**：探索/设计与实施是两种阶段，用户以显式指令切换。重大取舍先出方案表让用户拍板，
   拍板结果写入 BRIEF/apply_verdicts/文档，不靠口头记忆。
2. **闭环节奏**：总结讨论 → 落码 → 全量回归 → 文档回填（docs/08 里程碑 + docs/03 ADR + 问题总账）→ 提交推送。
3. **验收口径**：代码完成 ≠ 里程碑完成；需要真机验收的（新开关、生成链路改动）由 harness 脚本
   （novel_workspace/_harness/run_*.py）跑真实项目 + 人工审查 7 项清单。
4. **需求口径**：用户说"写 x 章"= **长篇小说的前 x 章**（规划 3 卷、只写第 1 卷前 x 章），
   绝不把完整弧线压缩进 x 章完结（guidan5 教训）。
5. **用户可接受更多模型调用换更高质量**；遇关键缺陷/spec 缺失优先打断汇报，非严重问题默认继续。
6. 提交信息风格：`feat(M3x)/fix(scope): 中文一句话摘要 + 要点列表`；一次提交一批同主题工作。
7. harness 探针脚本入库（是排查证据链）；`novel_workspace/<pid>/` 项目数据、`*.index.db`、pyc 不入库；
   测试一律 tmp_path，绝不写死真实项目路径。
8. **外部协作改动收编流程**：先 `git status/diff` 全量取证 → 新模块读 docstring → docs/08 diff 看
   里程碑记录 → 跑全量回归 → 单提交入库（参考 de08e71）。

## 6. 当前进度快照（2026-09-11 更新）

- **已闭环（勿重做）**：bible 注入、事件抽取、线索子系统批1+批2（ADR-025）、商讨轮扩展 M3u、
  M3v 事件级实然回写完全体、双 JSON 清污 F8、承诺账本 M3w / 记忆 rerank M3x / 任务板 M3y
  （ADR-026/027/028）、**Provider 单轨工厂 + 多模型接入 + .env key 管理（P0-2 闭环，0840735）**、
  **M3z 设定集人工反馈通道（ADR-029 批次A ✅）+ 草稿溯源（ADR-030 批次B ✅）**、
  **M3aa 判断型子代理 Agent 化（ADR-032 F0-F2 ✅：chronicler 证据仲裁环 + 审校 Agent）**、
  **ADR-033 Forge 硬边界规模适配（A/B ✅）**、`novelist console` 交互中枢 + `forge shell` 常驻会话壳、
  云端时代残留清除（SSH/tunnel/freetoken/lmstudio 删码）。
- **fame5 真机验收**（`proj-fame5-20260906-124016`，测试书《出名就变强》）：DeepSeek 10 章完成、
  7 项清单过；6 项人工审查问题全部归因完（`_harness/前两章问题归因报告.md` + 总账 F1-F8）。
  遗留正文修复（见下节 P0）。
- 测试基线：**966 passed / 2 skipped**（2026-09-11 实测，含 fastembed 本地向量缓存）。

## 7. 接下来做什么（按优先级）

| 优先级 | 任务 | 说明 |
| --- | --- | --- |
| **P0** | **ADR-031 批次C 编码** | 人工修订识别 + 归因回写（设计已成文，草稿溯源 `.src.json` 已铺底，待接线） |
| **P0** | fame5 待拍板 + 正文批量 revise | 开局修为定值二选一（细纲"炼气三层废柴" vs 正文考核"九层压线"）——**阻塞中，等用户拍板**；拍板后一次 revise 完成：载体统一（骨牌/玉简→系统一体）/修为线理顺/1-2 恶名交代 |
| **P0** | deepseek-v4-flash 真机验收 | 默认模型已切 flash + Provider 已多厂商化，但缺一轮真机生成验证：harness 跑 3-5 章确认质量/思考行为/预算 |
| P1 | fastembed 缓存持久化 | 现落 `%TEMP%/fastembed_cache`（易被清理、离线环境不可用）；建议 `LocalEmbedding` 默认 `cache_dir` 指工作区持久目录 |
| P1 | LocalEmbedding 惰性降级穿透 | `make_embedding("local")` 构造期降级捕获不到首次调用加载失败 → 无模型环境崩而非降级（见总账 P-FE2） |
| P1 | bible skill 卡系统条款 | 系统与宿主一体无私寄物件 / 善恶双值数值定义 / 机制私有性 + 反例"常人无声望机制" |
| P1 | apply_verdicts 拍板面扩 4 项 | 载体 / 数值体系 / 开局修为 / 机制私有（商讨轮可拍板的字段进 seed） |
| P1 | 一致性引擎加"层数越界"拦截 | 境界 9 小阶上限进词表（1-1"炼气十一层"穿帮教训） |
| P1 | F4 ChroniclerReport 透传 | 线索三字段 + checkpoint/audit ran 标记进 ProductionResult（harness 才能逐章观测） |
| P2 | 线索阶段6 真机验收 | 多线新需求走新 pace/romance/opening 槽位，5 章 7 项清单 |
| P2 | 批次二方案1 反AI味量化引擎 | 风格治理从"润色"升级为可度量 |
| P2 | M3x.R3 / M3y.T3 编排层接线 | rerank 与任务板接入编排层（模块已编码，默认关）；console 后续阶段（读章/修订建议链/大纲审核） |
| P2 | worldstate.time / 章级 prompt 顶层输出 / wuwu 绑定 canonical / ADR-021 v2 / relationships 事件性语义 / P1#7-9 / 七子代理收编拍板 | 中期池，按 docs/08 细则推进 |

## 8. 已知坑（AI 助手特别注意）

- **沙箱/文件操作**：禁 `rm` / `Path.unlink`（`rm -f A && B` 断链很隐蔽）；清空文件用 `: > file`；
  长任务用后台运行；命令工作目录会漂移，用显式绝对路径。
- **个人文件安全**：Desktop/Downloads 等目录只读扫描、先备份后动、走回收站——详见运行环境的
  安全策略（这些规则优先于任何任务描述）。
- **pytest 输出**：orchestrator 的 safe-delete 日志会淹没 stdout——重定向后 grep。
- **双 JSON 历史**：外部工具写细纲必须剥旧 frontmatter（F8 教训）；系统内写路径已全部核查为
  替换式，兜底 `bible.normalize_gist_frontmatter` / `normalize_all_gists`。
- **SceneBus 锁不可重入**（threading.Lock）：持锁不得再进加锁方法，超时分支在锁内直接标记 deny。
- **检索打分**：关键词路径绝不用定长哈希向量余弦（碰撞噪声盖真实信号），走精确 token 集合 + IDF
  加权余弦；语义模式才走向量。
- **旧项目兼容**：worldstate `baselines` 用 setdefault 防覆盖；rules.py R-STATE 读 baselines、
  旧项目 fallback 角色卡——改这两处会连带 10 条 event_sync 测试。
- **fastembed / embedding（2026-09-11 新增坑）**：
  1. 模型缓存在 `%TEMP%/fastembed_cache`（**易被清理**）；缓存缺失时首次调用要下载 ~550MB，
     下载在**沙箱代理下会 502**（本机实测）。预下载姿势：
     `https_proxy= http_proxy= HF_ENDPOINT=https://hf-mirror.com HF_HUB_DISABLE_SYMLINKS=1 python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='nomic-ai/nomic-embed-text-v1.5').embed(['x'])"`
     （Windows 上 HF 的 symlink 落位会失败，必须带 `HF_HUB_DISABLE_SYMLINKS=1`）。
  2. **降级链有洞**：`make_embedding("local"/"auto")` 的降级发生在构造期，而 `LocalEmbedding`
     是惰性加载——首次 `embed()` 才下载模型，失败时抛 `ProviderError`（构造期 try/except 捕获不到）。
     无模型/无网环境下 CLI 会崩而非降级（`test_m3_full_pipeline` 实测）。详见总账 P-FE。
- **yelan 系列已退役**（proj-yelan3，2026-09-04 拍板），别再拿它当测试书；现役测试书 = fame5。

---

*维护约定：重大拍板（模型/环境/流程变更）发生时更新本文档对应小节并在文档头追加修订日期。*
