"""事件总线与审计日志（docs/07 §4）。

进程内事件流；所有子系统发布、交互层/日志/统计订阅。事件纲目见 schemas/events.schema.json。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

Handler = Callable[["Event"], None]


@dataclass
class Event:
    ts: str
    kind: str
    seq: int
    session: dict[str, str]
    payload: dict[str, Any] = field(default_factory=dict)
    # host/record 扩展字段预留


class EventBus:
    """进程内事件总线（docs/07 §4）。thread-safe。"""

    def __init__(self) -> None:
        self._handlers: list[Handler] = []
        self._seq = 0
        self._lock = threading.Lock()

    def subscribe(self, handler: Handler) -> None:
        self._handlers.append(handler)

    def publish(
        self, kind: str, session: dict[str, str] | None = None, payload: dict[str, Any] | None = None
    ) -> Event:
        with self._lock:
            self._seq += 1
            seq = self._seq
        ev = Event(
            ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            kind=kind,
            seq=seq,
            session=session or {},
            payload=payload or {},
        )
        for h in list(self._handlers):
            h(ev)
        return ev
