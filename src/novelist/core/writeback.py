"""事件驱动实时回写（docs/04 §4.2 / docs/05 §5.4 / ADR-013）。

正文严格串行逐章推进；每个事件落定即以"事件回写"为单位，把该事件导致的人物经历、
剧情事件、关系变化、伏笔流转、时间推进实时固化到记忆层（不等章末）。

状态机（docs/06 §4.4）：
  event_landed → validated(冲突双检) → indexed(rag 增量) → archived
                     └ contradicted → 人工仲裁 → validated | 丢弃

冲突双检：规则层（引用完整性）在本地完成；语义层（LLM）由编纂员子代理完成（M2 起）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .session import SessionInfo
from ..storage.workspace import Workspace, WorkspaceError


@dataclass
class LandedEvent:
    """一个已落定事件（作为回写单元）。"""

    project_id: str
    vol: int
    ch: int
    seq: int              # 项目内事件序列号（project.json.event_seq）
    kind: str             # conflict|discovery|reveal|turning_point|dialogue|chapter|...
    summary: str
    participants: list[str] = field(default_factory=list)
    affected_threads: list[str] = field(default_factory=list)
    timeline_delta: dict[str, Any] | None = None
    state_delta: dict[str, Any] | None = None  # 人物状态增量


class WritebackError(Exception):
    pass


class ContradictionError(WritebackError):
    """规则层冲突（引用不完整等），须人工仲裁（docs/06 §4.4 contradicted）。"""


def commit_event(event: LandedEvent, session: SessionInfo, ws: Workspace | None = None, validate: bool = True) -> bool:
    """提交一个事件回写（docs/05 §5.4 第 1-3 步）。

    - validate=True：执行规则层冲突双检（引用完整性：参与者必须已建档）。
    - 通过后把本事件追加到 memory/plot_events.json 与相关人物经历史。
    - 冲突抛 ContradictionError；参数缺失抛 WritebackError。
    """
    if not event.summary:
        raise WritebackError("event summary is required")
    if ws is None:
        # 未绑定工作区时视为仅契约校验通过（供纯逻辑测试）
        return True

    if validate:
        _rule_check(ws, event)

    _append_plot_event(ws, event)
    _append_experiences(ws, event)

    # project.json.event_seq 递增（供下一事件计数）；这里简化：不强制，交由编排器。
    return True


def _rule_check(ws: Workspace, event: LandedEvent) -> None:
    """规则层冲突检查（docs/06 §5.2 引用完整性）。"""
    if not event.participants:
        return  # 无参与者则跳过（如纯旁白推进）
    p = ws.bible_path(event.project_id, "characters")
    if not p.exists():
        raise ContradictionError("participants referenced but characters.json missing")
    try:
        chars = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as e:  # pragma: no cover
        raise ContradictionError("characters.json invalid") from e
    known = {c.get("id") for c in chars} if isinstance(chars, list) else set()
    missing = [c for c in event.participants if c not in known]
    if missing:
        raise ContradictionError(f"事件参与者未建档: {missing}（需先创建人物卡，人工仲裁）")


def _append_plot_event(ws: Workspace, event: LandedEvent) -> None:
    """把事件追加到 memory/plot_events.json（docs/06 §3.5）。"""
    path = ws._abs(f"{event.project_id}/memory/plot_events.json")
    events = []
    if path.exists():
        try:
            events = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(events, list):
                events = []
        except ValueError:  # pragma: no cover
            events = []
    events.append(
        {
            "id": f"ev:{event.project_id}:{event.vol}:{event.ch}:{event.seq}",
            "at": {"vol": event.vol, "ch": event.ch},
            "type": event.kind,
            "summary": event.summary,
            "participants": event.participants,
            "affected_threads": event.affected_threads,
        }
    )
    ws.write_json(path, events)


def _append_experiences(ws: Workspace, event: LandedEvent) -> None:
    """为每个参与者追加一条人物经历（docs/06 §3.5）。"""
    for char_id in event.participants:
        path = ws.char_history_path(event.project_id, char_id)
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:  # pragma: no cover
                data = {}
        entries = data.get("entries", []) if isinstance(data, dict) else []
        entries.append(
            {
                "at": {"vol": event.vol, "ch": event.ch},
                "summary": event.summary,
                "state_delta": event.state_delta,
            }
        )
        payload = {"char_id": char_id, "revision": data.get("revision", 0) + 1 if isinstance(data, dict) else 1,
                   "entries": entries}
        ws.write_json(path, payload)
