"""事件驱动实时回写（docs/04 §4.2 / docs/05 §5.4 / ADR-013）。

正文严格串行逐章推进；每个事件落定即以"事件回写"为单位，把该事件导致的人物经历、
剧情事件、关系变化、伏笔流转、时间推进实时固化到记忆层（不等章末）。

状态机（docs/06 §4.4）：
  event_landed → validated(冲突双检) → indexed(rag 增量) → archived
                     └ contradicted → 人工仲裁 → validated | 丢弃

冲突双检（docs/06 §4.4）：
- 规则层：参与者引用完整性（本模块）+ 重复入库 / bible 引用完整性（core.memory.MemoryWriter）。
- 语义层（LLM）：经 `semantic_checker` 回调注入（编纂员子代理，docs/05 §5.4 第 4 步）；
  未注入时跳过，仅做规则层双检。

回写落到记忆层的同时**增量更新 RAG 索引**，使下一事件/下一章立即可"先忆"（F11.4/F11.5、A9）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .memory import MemoryConflictError, MemoryWriter
from .session import SessionInfo
from ..storage.workspace import Workspace


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


def commit_event(
    event: LandedEvent,
    session: SessionInfo,
    ws: Workspace | None = None,
    validate: bool = True,
    *,
    embedding=None,
    semantic_checker=None,
) -> bool:
    """提交一个事件回写（docs/05 §5.4 第 1-3 步）。

    - validate=True：执行规则层冲突双检（引用完整性：参与者必须已建档）。
    - 通过后把本事件追加到 memory/plot_events.json 与相关人物经历史，并增量更新 RAG 索引。
    - 冲突抛 ContradictionError（含记忆层冲突与语义层冲突）；参数缺失抛 WritebackError。
    - `semantic_checker(new_text, existing_texts) -> bool|None`：注入后启用语义层双检。
    """
    if not event.summary:
        raise WritebackError("event summary is required")
    if ws is None:
        # 未绑定工作区时视为仅契约校验通过（供纯逻辑测试）
        return True

    if validate:
        _rule_check(ws, event)

    writer = MemoryWriter(
        ws,
        event.project_id,
        embedding=embedding,
        semantic_checker=semantic_checker,
    )
    # 同章合成事件消歧（2026-09-04 实证 proj-20260903194907：失败轮留下的
    # 「完成第X卷第Y章」与新轮真实事件并存污染 plot_events/RAG）：
    # - 来者是真实事件 → 同章旧合成事件已过时（设计上二者互斥，orchestrator:1737
    #   有真实事件就不写合成），清掉；
    # - 来者是合成事件 → 幂等重写，防重跑叠加。
    writer.drop_synthetic_chapter(event.vol, event.ch)
    try:
        writer.append_plot_event(
            {
                "id": f"ev:{event.project_id}:{event.vol}:{event.ch}:{event.seq}",
                "at": {"vol": event.vol, "ch": event.ch},
                "type": event.kind,
                "summary": event.summary,
                "participants": list(event.participants),
                "affected_threads": list(event.affected_threads),
            }
        )
        for char_id in event.participants:
            writer.append_experience(
                char_id,
                {
                    "at": {"vol": event.vol, "ch": event.ch},
                    "summary": event.summary,
                    "state_delta": event.state_delta,
                },
            )
    except MemoryConflictError as e:
        # 记忆层/语义层冲突 → 回退并提请人工仲裁，不静默入库（docs/06 §4.4 contradicted）
        raise ContradictionError(str(e)) from e

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

