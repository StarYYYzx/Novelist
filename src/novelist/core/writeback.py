"""事件驱动实时回写（docs/04 §4.2 / docs/05 §5.4 / ADR-013）。

正文严格串行逐章推进；每个事件落定即以"事件回写"为单位，把该事件导致的人物经历、
剧情事件、关系变化、伏笔流转、时间推进实时固化到记忆层（不等章末）。
本模块定义事件回写的入口契约与最小实现；编纂细节（冲突双检/索引）由 memory 与
后续 M1/M2 填充。

状态机（docs/06 §4.4）：
  event_landed → validated(冲突双检) → indexed(rag 增量) → archived
                     └ contradicted → 人工仲裁 → validated | 丢弃
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .session import SessionInfo


@dataclass
class LandedEvent:
    """一个已落定事件（作为回写单元）。"""

    project_id: str
    vol: int
    ch: int
    seq: int              # 项目内事件序列号（project.json.event_seq）
    kind: str             # conflict|discovery|reveal|turning_point|dialogue|...
    summary: str
    participants: list[str] = field(default_factory=list)
    affected_threads: list[str] = field(default_factory=list)
    timeline_delta: dict[str, Any] | None = None
    state_delta: dict[str, Any] | None = None  # 人物状态增量


class WritebackError(Exception):
    pass


def commit_event(event: LandedEvent, session: SessionInfo, validate: bool = True) -> bool:
    """提交一个事件回写。

    校验通过经 MemoryWriter 写入 memory/ 并归置索引；冲突或参数缺失抛 WritebackError。
    这是"每完成一个事件即实时更新记录"的入口（docs/05 §5.4 第 1-3 步）。
    """
    if not event.summary:
        raise WritebackError("event summary is required")
    # 冲突双检（规则+语义）与 memory 写入、rag 增量交由 memory 子系统实现（M1/M2）。
    # 此处仅为契约桩：在 validate 模式返回 True，供流程串起来。
    return True
