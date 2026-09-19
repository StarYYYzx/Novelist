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


@contextmanager
def heartbeat(label: str, *, after_s: float | None = None,
              interval_s: float = 5.0) -> Iterator[None]:
    """长操作心跳（UX-2，2026-09-19）：超过阈值未完成就每 interval_s 报一次「仍在进行」。

    背景：构建/生成的单次 LLM 调用实测 15–21s（book 节点），期间零输出，用户无从判断
    是"在跑"还是"卡死"。心跳用**守护线程 + Event** 实现：不碰 provider、不引入流式，
    零风险止血（真 streaming 属 ADR-036 第二版）。

    - `after_s`：首跳阈值，缺省读 `NOVELIST_HEARTBEAT_S`（默认 8s；<=0 立即起跳，测试用）。
    - 心跳异常绝不打断主流程（全部吞掉）；线程 daemon=True，进程退出不留尾巴。
    - sink 是 ContextVar，**心跳线程不继承上下文**——进入时显式捕获当前 sink。
    """
    import os
    import threading
    import time

    if after_s is None:
        try:
            after_s = float(os.environ.get("NOVELIST_HEARTBEAT_S", "8"))
        except (TypeError, ValueError):
            after_s = 8.0
    after = max(after_s, 0.01)
    sink = _SINK.get()
    stop = threading.Event()
    started = time.monotonic()

    def _tick() -> None:
        if stop.wait(after):
            return  # 阈值内完成 → 一跳都不发
        while not stop.wait(interval_s):
            line = f"  …{label}仍在进行（已 {fmt_duration(time.monotonic() - started)}）"
            try:
                if sink is not None:
                    sink(line)
                else:
                    emit(line)
            except Exception:  # noqa: BLE001 - 心跳失败绝不打断主流程
                return

    t = threading.Thread(target=_tick, name="novelist-heartbeat", daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join(timeout=1.0)
