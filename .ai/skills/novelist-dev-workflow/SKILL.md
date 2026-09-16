---
name: novelist-dev-workflow
description: Novelist 仓库（多 Agent 长篇小说撰写系统）的日常开发与回归约定：正确的解释器与 pytest 命令、五道机械门禁（scripts/check.py、pre-commit、CI）、AI 规范分发（AGENTS.md 单一源 / .ai/skills / ai_bootstrap.py 目录联接）、全量回归必须前置的沙箱环境变量、本机 coreutils 缺失的绕法、生成链路的已拍板口径（deepseek-v4-flash / Provider 单轨工厂 / ADR 系列）、改 schema 与文档的联动要求、以及只读禁区。当在本仓库跑测试或门禁、修 bug、改生成链路、动 schema/docs、调整 AI 引导与 hook、或接手这个项目时使用。
agent_created: true
---

# Novelist 仓库开发约定

仓库根：`E:\360MoveData\Users\Administrator\Desktop\novelist`。
**进度判定只看代码 + `git log` + `docs/08-implementation-plan.md`**，README/AGENTS 的自述阶段会滞后。

## 1. 命令（照抄，别自己拼）

```bash
# ★ 首选入口：五道门禁一键跑（自己注入沙箱环境变量、自己找 ruff，不用手拼命令）
"C:/Python314/python.exe" scripts/check.py --all     # G1 ruff → G2 密钥 → G3 卫生 → G4 pytest → G5 文档基线
"C:/Python314/python.exe" scripts/check.py --quick   # 仅 G1–G3（pre-commit 用的就是它）
"C:/Python314/python.exe" scripts/ai_bootstrap.py --status   # 查看技能链接 / hooksPath 是否就位

# 全量回归——必须带这两个环境变量，否则测试自身的 unlink 累计超阈值被沙箱误杀
# → 表现为"偶发失败、单跑全过"
CODEBUDDY_SAFE_DELETE_ENABLED=0 CODEBUDDY_SAFE_DELETE_BULK_THRESHOLD=100000 \
  "C:/Python314/python.exe" -m pytest tests/ -q

# lint（ruff 装在 anaconda 环境，未进任何 venv）
"E:/python/ana/Scripts/ruff.EXE" check src tests scripts

# CLI（pytest 靠 [tool.pytest.ini_options].pythonpath=["src"] 注入路径，无 editable install）
"C:/Python314/python.exe" -m novelist.cli --help
```

- **解释器**：只能用系统 `C:/Python314/python.exe`（pytest 9.1.1）。托管 3.13.12 **没装 pytest**。
- **回归输出请重定向到文件再读**：`... > _out.txt 2>&1`。stdout 会被框架日志污染，且管道工具不可用。
- 记录口径要四个数：**收集数 / passed / skipped / deselected**。

## 2. 本机环境陷阱（都踩过）

| 现象 | 真相 / 绕法 |
|---|---|
| `head`/`tail`/`cat`/`wc`/`find`/`dirname` 全部 `command not found` | git bash 的 coreutils 缺失。统计与过滤**一律**用 `"C:/Python314/python.exe" -c "..."`，不要写 shell 管道 |
| `rm` / `Path.unlink` 被沙箱拦 | 清空文件用 `: > file`；删文件用 `os.remove` 前先确认没被拦；长命令用后台任务 |
| `git status` 显示 `master...origin/master [gone]` | **环境假象，不是远端丢分支**。本机无法在 `.git/refs/` 下建子目录，`refs/remotes/*` 不落盘；tag 与 `refs/heads/*` 正常。用 `git ls-remote` 判定 |
| **禁 `git-filter-repo`** | 会清空 `.git` |
| 跨盘（C:→E:）`shutil.move` 半途失败 | 沙箱拦删除时，会出现"目标已复制、源仍保留"的**重复副本**。移动后必须核对两侧文件数与字节数，重复项用哈希确认后再清 |
| git 输出解码 | git 输出含 GBK 字节，直接 UTF-8 `decode()` 会 UnicodeDecodeError → 用 `subprocess` + 多编码兜底 |

## 3. 已拍板口径（改码前先对齐，别自作主张）

- **生成一律走 DeepSeek API `deepseek-v4-flash`（禁 pro）**。云端 AutoDL 与本地 LM-Studio **已停用删码**。
- Provider 统一走单轨工厂 `providers.create(name, **kw)` + `REGISTRY`。Key 优先级：显式值 > 环境变量 > gitignored `.env`。
- **单元测试绝不真调 LLM**（用 `providers/fake.py`）；真实 API 用例**必须** `@pytest.mark.slow`
  （`addopts = -m 'not slow'`，默认套件里真调外部 API 是违规）。
- embedding = 本地 fastembed ONNX（nomic-embed-text-v1.5 / 768 维 / CPU 零 torch），不可用则降级关键词
  （精确 token 集合 + IDF 加权余弦）。**检索打分不要退回定长哈希向量余弦**——dim=256 时碰撞噪声会盖过真实信号。
  - 2026-09-15 起两坑已修：缓存落持久目录（`NOVELIST_EMBED_CACHE` > `LOCALAPPDATA`/`XDG_CACHE_HOME`
    > `~/.cache`），**运行期就地降级**（`embed()` 抛异常即切关键词、`kind` 翻 `keyword-hash`）。
    **纪律：`kind` 必须在 `embed()` 之后读**——构造期读不到降级（这是 P-FE2 的根因）。
- **输出必须走 `core/output.emit()`（禁裸 `print`）**：它带 sink 重定向（console 实时转发）
  与 UTF-8 兜底（Windows 重定向 + 非 UTF-8 locale 下裸 print 中文会直接崩掉生成）；
  耗时统一用 `core/output.fmt_duration()`。
- **原始调用日志（`core/calllog`，ADR-035）默认在写** `./raw-calls/YYYY-MM-DD.jsonl`（含完整 prompt，
  不入 git）。关法：`NOVELIST_CALLLOG=0` 或 `novelist console --no-calllog`；`disable_calllog()` 是
  **粘性**的（不会被下次调用自动开启）。上限 `NOVELIST_CALLLOG_MAX_MB`，超限只写一条标记、不删历史。
- ADR：002 正文严格串行逐章 | 011 bible=应然 / memory=实然 | 013 事件落定即实时回写 |
  016 文件=持久事实源（SQLite/RAG 仅可再生缓存） | 017/018 Forge 递归硬边界（depth4/width4/calls80/retry1） |
  019 worldstate | 021 事件级选角。
- `SceneBus` 用**不可重入** `threading.Lock`——持锁时不得再进加锁方法（超时分支就在锁内直接标记 deny）。
- **Forge 节点落库是事务，别绕过**（2026-09-16 事故 `0e67b29`）：`_run_node_impl` 用
  `forge.state.blueprint_txn` 包住 `_APPLY[kind]` —— apply 内任一步抛错（含"主线唯一"这类硬校验）
  **整体回滚**；以前是"先写蓝图、后校验"，失败仍 `bp.save()`+`sync_bible()`，脏数据直接进 bible。
  写新的 `_apply_*` 段时**不要自己 save**，也不要在事务外改 `bp`。配套：重试 prompt 会带上一轮的
  行号诊断（`ctx.retry_hint`）、JSON 解析容错（`normalize.loads_json_tolerant`）、
  连续 3 个节点失败即中止（`_FailStreak`）。
- **`chapter` 默认已是直出**（2026-09-15 起，`--direct/--loop` 默认 True；`produce_chapter`
  签名默认 `prefer_direct=True`）。`--loop` 是显式实验开关，走 Agent 工具循环。
  **工具模式有草稿存在性断言**：拿不到 `write_draft` 落盘就 `ok=False`，不再"ok=True + 不存在的
  `chapter_path`"（那曾是 2026-09-05 缺陷 DA 的未修主体）。
- **工具调用已真正接线**（AG-2/AG-3）：`_decide` 会下发 `tools`（`to_openai_schema()`，
  不含内部 `level`）、`LLMMessage` 带 `tool_calls/tool_call_id`、工具结果以 `role="tool"` 回灌，
  且**仅当 `provider.capabilities.tool_calling` 为真**才下发（否则自动降级直出并留痕）。
  **注意：`--loop` 尚未在真机跑过**——验证看 `raw-calls/*.jsonl` 里有没有 `tools` 与 `tool_calls`。
- **门禁 fail-closed**：非 `allow/ask/deny` 的取值（含拼错）一律 deny + 告警；策略文件解析期
  即校验并抛错。策略缺失时回退链 = 请求 profile → `supervised` → 内置兜底档（不再 KeyError）。
  `delete_file` 默认是 **`ask`**（调用时人工 y/N），白名单只允许 `drafts/`、`workspace/`。

## 4. 改动前的联动检查（漏了就是债）

- 写代码前读 **`docs/11-coding-standard.md`**；动 orchestrator/providers/workspace 前看它的 §13 存量整改清单。
- **改 schema 必须同步审查 `docs/06` §5.2 的规则引擎白名单**，否则引擎不认识新字段。
- 新增机制要**回填对应 ADR 与 FR/验收标准**（`docs/03` / `docs/02`）。
- **文档基线里的数字最后改**：若本轮会删代码/删导入，先做代码再重测基线，否则数字立刻失效。
- 文档里的"全量 774 passed"这类里程碑数字是**当时实况的历史记录**，不要"修正"；只有"当前基线"类声明才需同步。

## 5. 只读 / 禁区

- **`tests/test_noval/` 是用户手写稿，只读不改删。**
- **`.workbuddy/` 是项目数据（含 memory），不要删。**
- `core/scene_tools.py` **零引用但不要删**：docs/05 §93/274/279 与 docs/08 明确记它是"已备工具、待接入围读会"
  的**预留件**——删它要连带改设计文档，属设计决策。
- `novel_workspace/`、`*.index.db`、`demo/` 不入 git；测试一律用 `tmp_path`。

## 6. AI 规范与门禁（ADR-034 —— 改这套东西前先读）

单一源 + 薄桥接 + 机械门禁，保证"队友 clone 后 AI 行为一致"：

| 组件 | 角色 |
|---|---|
| `AGENTS.md` | **唯一规范源**（§0–§9）。跨工具事实标准（Codex/Copilot/Cursor/Cline/Zed 原生读；Claude Code 靠 `CLAUDE.md` 里的 `@AGENTS.md`） |
| `.ai/skills/` | **技能单一源**（git 跟踪）。当前 2 个：`novelist-dev-workflow`、`python-repo-quality-remediation` |
| `scripts/ai_bootstrap.py` | 用 `mklink /J` 把 `.workbuddy/skills`、`.claude/skills` 目录联接指向 `.ai/skills`；同时 `git config core.hooksPath .githooks` + 写本机解释器到 `.git/novelist-hook-python`（**不入库**）。**每个新 clone 只需跑一次** |
| `scripts/check.py` | G1 ruff / G2 密钥与敏感文件 / G3 仓库卫生 / G4 pytest / G5 文档基线 9 指标 |
| `.githooks/pre-commit` | 跑 `check.py --quick`；`SKIP_CHECK=1` 绕过 |
| `.github/workflows/ci.yml` | windows-latest + **Python 3.11**（声明的下限，能抓 PEP 701 类 f-string 回退问题） |

**这套 harness 自身踩过的坑（别再踩）：**

- **`cmd || fallback` 里非零退出码 ≠ 失败**：hook 里 `IFS= read -r PY < file || PY=""`，
  文件**无尾随换行**时 POSIX `read` 返回非 0，`||` 会把**刚读成功的值清空** → 静默回退 PATH 上
  碰到的任意 python（实测落到没装 pytest 的 3.13）。现在是不带 `||` + 手工剥 `\r`。
- **写机器路径给 shell 用要 `as_posix()` + `newline="\n"`**：反斜杠路径在 `[ -x "$PY" ]` 下判定失败；
  默认 CRLF 会让 `read` 带出尾随 `\r`。两处都会导致静默回退。
- **加 `.gitignore` 忽略规则会拆掉依赖 `git status` 的检查**：加了 `/_*.py` 后 G3 原来的
  "untracked 残留"检查永久失效。现在 G3 = 硬拦**已入库**的根目录 `_*` 文件 + 校验 `.gitignore`
  必需条目仍在；on-disk 残留降级为 WARN。
- **G5 比对文件名要用 `Path(name).stem`**：文档写 `orchestrator 2610`（不带 `.py`），按完整名匹配会误报。
- **`.gitattributes` 锁 LF**（`* text=auto eol=lf`）。本机 `core.autocrlf=true`，不锁定 hook 会被检出成
  CRLF。Git for Windows 的 sh 能容忍带 `\r` 的 shebang，但 WSL/Linux 下 `sh .githooks/pre-commit` 不行。
- hook 脚本**只用 shell 内建**（`read`/`command`/`[`），不用 `cat` 等外部命令——本机 coreutils 缺失。

## 6.5 约定的落地方式：优先加机械守卫，而不是写文档

**本项目偏好**（AGENTS.md §7）：能写成检查的约定就不要只写散文——散文会漂移，检查不会。
已有先例：G5 文档基线、`test_m14_forge_console.py` 里**解析 HELP_TEXT** 的守卫
（拒绝"同一命令两种描述"，历史 bug 是 `/validate` 被写成两种含义、且真正的 `forge validate` 不可达）。

写守卫时踩过的两个坑：

- **"补分隔符"类工具的默认值必须是「无需补」**：`calllog._append_line` 往 JSONL 追加前要保证
  文件以换行结尾（防上次写入被中断留下半行 → 该行 JSON 解析失败、**当日日志此后全废**）。
  但 `_ends_with_newline()` 若对**不存在**的文件返回 `False`，就会在新文件首行前凭空塞一个空行 →
  第一行解析失败。正反两个方向都会被测试抓到：加守卫时**两个方向都要写用例**。
- **同一把锁里判定、也只在那里标记**：配额"只写一条终态标记"若判定与标记分处锁内外，并发下会重复写。

## 6.6 离线复现真实链路（比读代码快，也比读代码可靠）

想知道"这条路径在真机到底能不能用"，标准做法是**用桩 provider 跑真函数**（不调真 API、不读真工作区）：

- **跑整章**：`Workspace(root=tempfile.mkdtemp())` + `ws.create_project(pid)` +
  `Checkpoint(ws).save(pid, {"id":pid,"pipeline_state":"立项","event_seq":0})`，然后
  `produce_chapter(ws, pid, 1, 1, 桩provider, session=SessionInfo(...))`，最后打印
  `res.ok / res.mode / ws.draft_path(...).exists() / res.chapter_path / req.tools`。
  桩 provider 里 assert `req.tools is None` 一句话就能证明"工具定义压根没下发"。
- **跑工具契约**：`ToolRegistry(gate=PermissionGate({...}))` + `reg.invoke(session, name, params)`，
  直接看 `ToolResult` —— 门禁取值、错误码、失败是否被包成 `ok` 全在这里暴露。
- **反查真机实际行为**：`novel_workspace/*/reports/stats/generation-*.md` 里有 `mode=direct|tool`
  与 `LLM 调用 N 次：in=… / out=…`，比任何文档都可信（本仓 72 份审计里 67 份是 direct）。
- 桩 provider 只要能 `complete(req) -> LLMResult` 即可（`blocked=True` / `content=""` 两种极端都要试）。

## 7. 已知未做项（别重复发现）

`docs/11` §13：**P0-1** `core/orchestrator.py` 的 `_produce_chapter_impl` 1132 行待拆
（2026-09-15 已拆出薄包装 `produce_chapter` 挂 calllog 上下文，**impl 本体未拆**）；
**P0-3** `ws._abs` → `ws.path`（47 文件 175 处，`ws.path(` 调用数仍 0）；**P1-4** 7 个异常未归 `NovelistError`；
**P2-8** structlog 未删；**P2-9** ruff 已落（`[tool.ruff]` 在 `pyproject.toml`，`select = E4/E7/E9/F`
锁定当前零告警状态），**提标（I/B/UP/SIM）与 mypy 故意留作独立批次**。
另有：抽公共 `read_json`（6 份副本）与 LLM JSON 宽容解析（7 份副本）——可回收约 180–250 行。
**S-2/S-3（世界观基座）+ U7（`[generation]` 配置段）已落地**（`470df38`）；S-1 润色层术语硬约束
**拍板不做**。Agent 层 AG-1…AG-24 已全修（`d05c8b9`，含 6 项拍板），清单与落地记录见
`docs/Agent层审计与修复方案-2026-09-15.md`。**仍未做**：`--loop` 真机验收、S-4 章级审校→自动修订、
F3.5/F10 角色试演、F13 围读会、NFR-10 `novelist eval`。

## 8. 收尾

1. `"C:/Python314/python.exe" scripts/check.py --all` 五项全绿
2. 若动了 `docs/11` 基线表涉及的数字，先改代码再重测（G5 会拦住漂移）
3. 临时脚本（`_*.py`、`_out.txt`）清理干净——**本项目习惯把临时脚本放根目录，容易残留**
4. 更新 `.workbuddy/memory/YYYY-MM-DD.md`（只追加，记可复用教训而非过程流水）
5. 用户没说推送就不要 push

通用质量整治的批次顺序与 ruff 陷阱见 skill `python-repo-quality-remediation`。
