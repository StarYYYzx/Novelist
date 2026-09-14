"""进度/提示输出的统一出口（D9，2026-09-15）。

背景：生成期进度原先直接 `print(..., flush=True)`，有两个实害：

1. **编码崩溃面**：Windows 上 stdout 被重定向（管道/文件）且 locale 非 UTF-8 时
   （CI 的 cp1252 runner 是典型），打印中文直接 `UnicodeEncodeError`，**把生成打断**
   ——与 `scripts/check.py` 修的是同一类问题。
2. **绕过 IO 抽象**：`forge console` 用 `CliRunner` 捕获 stdout，这些 print 会**攒到
   命令结束**才一次性刷出，"实时进度"在 console/server 模式下失效（U6）。

本模块提供两件东西：

- `emit(line)`：统一输出出口。绑定过 sink 时走 sink（console 实时转发），否则打印，
  并自带 UTF-8 兜底（编码不了就替换，绝不因输出失败打断生成）。
- `use_output(writer)`：上下文管理器，把 sink 绑为 `writer`。
- `fmt_duration(seconds)`：耗时格式化（`mm:ss`，超 1 小时 `h:mm:ss`）——U6 进度与耗时
  显示在 orchestrator 与 cli 两处共用，避免各自造一份格式。

sink 用 `ContextVar` 而非全局变量——随调用上下文传播，多线程/多会话互不串台。
注意：**sink 实现自身不得回调 `emit()`**（会自递归）；`forge/console.py` 的
`ConsoleIO.output` 因此直接写流。
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator

Sink = Callable[[str], None]

_SINK: ContextVar[Sink | None] = ContextVar("novelist_output_sink", default=None)


@contextmanager
def use_output(writer: Sink) -> Iterator[None]:
    """把 `emit()` 的输出重定向到 `writer`（退出自动还原）。"""
    token = _SINK.set(writer)
    try:
        yield
    finally:
        _SINK.reset(token)


def emit(line: str = "") -> None:
    """输出一行（或一段）进度：绑定 sink 时走 sink，否则打印；自动 flush。

    编码失败降级为 `errors="replace"`（宁可看到乱码字符，不可让中文输出把生成打断）。
    """
    sink = _SINK.get()
    if sink is not None:
        try:
            sink(line)
            return
        except Exception:  # noqa: BLE001 - sink 故障退回标准输出，进度不能因此中断
            pass
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        stream = sys.stdout
        enc = getattr(stream, "encoding", None) or "utf-8"
        print(line.encode(enc, "replace").decode(enc, "replace"), flush=True)


def fmt_duration(seconds: float) -> str:
    """秒 → `mm:ss`（>=1 小时则 `h:mm:ss`）；负数按 0 处理。

    U6：长任务进度（`已 03:41`）与收尾汇总（`用时 12:04`）共用同一格式。
    """
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"
