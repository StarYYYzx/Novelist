"""原始 LLM 调用日志（CallLog）——生成产物溯源第一现场。

**定位**：系统已有的审计（`reports/stats/generation-*.md`、`.index.db audit_log`、forge
`transcript.jsonl`）只记录**结构化结果与计数**，不含"这次调用 prompt 到底发了什么、模型
原样返回了什么"。本模块补上这一层原始记录：每次真实的 provider `complete()` 都追加一行
**JSONL**（完整 messages / 实际请求体 / 原始响应文本 / 解析结果 / 异常 / 耗时），供"发现
生成产物有问题时从完整记录溯源"。

**存放**：默认独立目录 `raw-calls/`（相对进程 CWD；可用 `NOVELIST_CALLLOG_DIR` 环境变量
或 `enable_calllog(dir)` 覆盖），按 `YYYY-MM-DD.jsonl` 分文件，逐行一个调用。不依赖具体
工作区，跨项目集中可查；**不入 git**（见 .gitignore `raw-calls/`）。

**设计约束**：
- 线程安全：多 Agent / 围读会并发写用 `threading.Lock` 串行 append（单行原子写）。
- 性能与磁盘安全：未启用时 `record()` 为 no-op；fake 等测试替身**不走**
  openai 拦截点，不会在测试里刷盘。
- 上下文：`call_context(label)` 上下文管理器把当前"正在做什么"（项目卷章/节点 kind）压栈；
  provider 记录时带上栈顶串，溯源时一眼定位到调用归属。

**开启与否（2026-09-15 修正记录）**：本文档原先声明"未 `enable_calllog` 时 no-op"（opt-in），
但 `providers/openai.py` 在 `finally` 里**首次真实调用即自动开启**（见 `ensure_enabled`）——
实现与自述不一致，且 `disable_calllog()` 会被下一次调用覆盖。**默认归属属拍板项**
（`docs/代码与逻辑复查-2026-09-15.md` §1.4#1），本轮只补两件不改归属的事：
① 提供**关闭开关** `NOVELIST_CALLLOG=0/off/false/no`；② `disable_calllog()` 后保持关闭
（不再被自动开启覆盖，语义见 `ensure_enabled`）。

**容量**：`NOVELIST_CALLLOG_MAX_MB` 限制目录总量（0/未设=不限）。超限后停止记录并
写一条终态标记行，不静默。**不自动删除历史文件**（删用户数据不做隐式动作）；
按日期清理请手工删 `raw-calls/YYYY-MM-DD.jsonl`。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

_DEFAULT_DIR = os.environ.get("NOVELIST_CALLLOG_DIR", "raw-calls")

# 显式关闭值（`NOVELIST_CALLLOG`）：关闭后 `ensure_enabled()` 不再自动开启。
_OFF_VALUES = {"0", "off", "false", "no", "none"}

_LOCK = threading.Lock()
_DIR: Path | None = None
# disable_calllog() 后置位：表示"用户/测试显式关闭过"，自动开启必须让位（否则
# 关不掉——这正是本轮修的口径问题）。enable_calllog() 显式开启时复位。
_USER_DISABLED = False
_QUOTA_STOPPED = False
_CTX_STACK: ContextVar[tuple[str, ...]] = ContextVar("novelist_calllog_ctx", default=())

# 日志里遇到形如真实密钥的值时用它替换（double 保险：payload/响应文本内一般无 key，
# 但异常串偶现拼接；与 providers/secrets.redact_message 语义一致）。
_REDACTION = "<redacted>"
_SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{8,})|(Bearer\s+\S+)")


def _env_off() -> bool:
    return os.environ.get("NOVELIST_CALLLOG", "").strip().lower() in _OFF_VALUES


def max_bytes() -> int:
    """目录容量上限字节数（`NOVELIST_CALLLOG_MAX_MB`，<=0 或非法 = 不限）。"""
    try:
        mb = int(os.environ.get("NOVELIST_CALLLOG_MAX_MB") or "0")
    except (TypeError, ValueError):
        return 0
    return mb * 1024 * 1024 if mb > 0 else 0


def enable_calllog(directory: str | Path | None = None) -> Path:
    """开启日志并返回目录（幂等重建）。目录缺省 `NOVELIST_CALLLOG_DIR` 或 `raw-calls/`。"""
    global _DIR, _USER_DISABLED, _QUOTA_STOPPED
    d = Path(directory or _DEFAULT_DIR).resolve()
    d.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        _DIR = d
        _USER_DISABLED = False
        _QUOTA_STOPPED = False
    return d


def disable_calllog() -> None:
    """关闭日志；此后 `record()` 回到 no-op，且**不再被自动开启**（直到显式 enable）。"""
    global _DIR, _USER_DISABLED
    with _LOCK:
        _DIR = None
        _USER_DISABLED = True


def ensure_enabled() -> bool:
    """首次真实调用触发的**自动开启**（幂等）。返回是否允许自动开启。

    三种情况不允许自动开启：`NOVELIST_CALLLOG` 显式关闭、调用方 `disable_calllog()` 过、
    已在开启态（此时返回 True）。**显式 `enable_calllog()` 始终生效**（显式 > 环境变量，
    与 providers/secrets 的 key 优先级同口径）。默认归属（自动开 vs 纯 opt-in）是拍板项，
    本函数只保证"关得掉"。
    """
    if _env_off() or _USER_DISABLED:
        return False
    if not is_enabled():
        enable_calllog()
    return True


def is_enabled() -> bool:
    with _LOCK:
        return _DIR is not None


def dir_path() -> Path | None:
    """当前日志目录（未启用返回 None）——供 CLI 回显"日志写到哪"，见 U8。"""
    with _LOCK:
        return _DIR


def current_ctx() -> str:
    """当前调用上下文链（外层→内层，` / ` 连接）；空则空串。"""
    stack = _CTX_STACK.get()
    return " / ".join(stack)


@contextmanager
def call_context(label: str):
    """把 `label`（如 "chapter 1-3"、"forge:character"）压栈，退出自动出栈。"""
    token = _CTX_STACK.set(_CTX_STACK.get() + (label,))
    try:
        yield
    finally:
        _CTX_STACK.reset(token)


def _dir_size(d: Path) -> int:
    total = 0
    for f in d.glob("*.jsonl"):
        try:
            total += f.stat().st_size
        except OSError:
            continue
    return total


def _ends_with_newline(p: Path) -> bool:
    """文件末尾是否为换行；**文件不存在或不可读一律返回 True**（即"无需补分隔符"）。

    新文件返回 False 会在首行前塞一个空行，使第一行 JSON 解析失败——这正是"补分隔符"
    这个修法自身最容易引入的反向 bug。
    """
    try:
        if not p.exists() or p.stat().st_size == 0:
            return True
        with p.open("rb") as f:  # 只读最后一个字节
            f.seek(-1, os.SEEK_END)
            return f.read(1) == b"\n"
    except OSError:
        return True


def _append_line(d: Path, rec: dict[str, Any]) -> Path:
    """写一行 JSONL。整条记录再过一次 `redact_secrets` 兜底（ADR-035 §D 的第二层）。

    追加前**必须保证文件以换行结尾**：若上一次写入被进程中断（半行残留），直接 append
    会把新记录粘在残行尾部 → 整行 JSON 解析失败、该日日志后续全部不可读。
    """
    p = d / f"{time.strftime('%Y-%m-%d')}.jsonl"
    line = json.dumps(rec, ensure_ascii=False, default=_default_json)
    need_sep = not _ends_with_newline(p)
    with p.open("a", encoding="utf-8") as f:  # 保证 async/多线程安全：字节之内原子追加
        if need_sep:
            f.write("\n")
        f.write((redact_secrets(line) or line) + "\n")
    return p


def record(entry: dict[str, Any]) -> Path | None:
    """追加一条调用记录（JSONL）；未开启时 no-op。返回落盘路径或 None。

    - 时间戳用**本地时间 + 时区偏移**，与文件名日期一致（原实现 `ts` 用 UTC、文件名用
      本地时区，北京时间 00:00–08:00 写出的记录日期会差一天，按日期 grep 错位）。
    - 记录全文再过一遍 `redact_secrets`（ADR-035 §D 宣称的"双保险"第二层，原先零调用）。
    - 超 `NOVELIST_CALLLOG_MAX_MB` 时停止记录，并写一条终态标记行（不静默）。
    """
    global _QUOTA_STOPPED
    with _LOCK:
        if _DIR is None:
            return None
        d = _DIR
        try:
            d.mkdir(parents=True, exist_ok=True)  # 目录被外部删掉时自愈，而不是静默停写
        except OSError:
            return None
        limit = max_bytes()
        if limit and _dir_size(d) >= limit:
            # 配额判定与"只写一次标记"在同一把锁内完成，避免并发下重复写标记行。
            if not _QUOTA_STOPPED:
                _QUOTA_STOPPED = True
                _append_line(d, {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
                    "calllog": "quota_exceeded",
                    "note": (f"raw-calls 已达 NOVELIST_CALLLOG_MAX_MB（{limit // (1024 * 1024)}MB）上限，"
                             "本次起停止记录；请清理历史 YYYY-MM-DD.jsonl 或调高上限后重启进程"),
                })
            return None
        rec: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
            "ctx": current_ctx() or None,
            **entry,
        }
        try:
            return _append_line(d, rec)
        except OSError:
            return None


def _default_json(o: Any) -> Any:
    """dataclass / Path 等 → 可 JSON 化（LLMResult/Usage/ToolCall via asdict 已处理）。"""
    if isinstance(o, Path):
        return str(o)
    return str(o)


def redact_secrets(text: str | None) -> str | None:
    """把形似密钥/ Bearer 令牌的串替换为占位符（防日志泄密；调用前做最后的兜底）。"""
    if not text:
        return text
    return _SECRET_RE.sub(_REDACTION, text)