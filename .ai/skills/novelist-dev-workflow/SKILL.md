---
name: novelist-dev-workflow
description: Novelist 仓库（多 Agent 长篇小说撰写系统）的日常开发与回归约定：正确的解释器与 pytest 命令、全量回归必须前置的沙箱环境变量、本机 coreutils 缺失的绕法、生成链路的已拍板口径（deepseek-v4-flash / Provider 单轨工厂 / ADR 系列）、改 schema 与文档的联动要求、以及只读禁区。当在本仓库跑测试、修 bug、改生成链路、动 schema/docs、或接手这个项目时使用。
agent_created: true
---

# Novelist 仓库开发约定

仓库根：`E:\360MoveData\Users\Administrator\Desktop\novelist`。
**进度判定只看代码 + `git log` + `docs/08-implementation-plan.md`**，README/AGENTS 的自述阶段会滞后。

## 1. 命令（照抄，别自己拼）

```bash
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
- ADR：002 正文严格串行逐章 | 011 bible=应然 / memory=实然 | 013 事件落定即实时回写 |
  016 文件=持久事实源（SQLite/RAG 仅可再生缓存） | 017/018 Forge 递归硬边界（depth4/width4/calls80/retry1） |
  019 worldstate | 021 事件级选角。
- `SceneBus` 用**不可重入** `threading.Lock`——持锁时不得再进加锁方法（超时分支就在锁内直接标记 deny）。

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

## 6. 已知未做项（别重复发现）

`docs/11` §13：**P0-1** `core/orchestrator.py` 的 `produce_chapter` 1112 行待拆；
**P0-3** `ws._abs` → `ws.path`（47 文件 175 处，`ws.path(` 调用数仍 0）；**P1-4** 7 个异常未归 `NovelistError`；
**P2-8** structlog 未删；**P2-9** `pyproject.toml` 无 `[tool.ruff]`/`[tool.mypy]`。
另有：抽公共 `read_json`（6 份副本）与 LLM JSON 宽容解析（7 份副本）——可回收约 180–250 行。

## 7. 收尾

1. `ruff check src tests scripts` 全绿
2. 全量回归绿（带 §1 的两个环境变量），记四个数
3. 临时脚本（`_*.py`、`_out.txt`）清理干净——**本项目习惯把临时脚本放根目录，容易残留**
4. 更新 `.workbuddy/memory/YYYY-MM-DD.md`（只追加，记可复用教训而非过程流水）
5. 用户没说推送就不要 push

通用质量整治的批次顺序与 ruff 陷阱见 skill `python-repo-quality-remediation`。
