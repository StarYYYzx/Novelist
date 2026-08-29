"""围读会（受控群聊）场景总线（docs/04 §4.3 / docs/05 §5.3 / docs/07 §7.5，ADR-014）。

主持人（主编剧/编排层）驱动一场 scene；演员经 join_scene/say_line/leave_scene 参与；
满足结束判据即收场（全体离场 / max_rounds / 话题收敛 / 预算超时 / 异常强制收场）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass
class RoundLine:
    actor: str
    speech: str


@dataclass
class Scene:
    scene_id: str
    topic: str
    actors: list[str]
    max_rounds: int
    transcript: list[RoundLine] = field(default_factory=list)
    rounds: int = 0
    active: set[str] = field(default_factory=set)
    closed: bool = False
    close_reason: str | None = None


class SceneBus:
    """进程内场景总线（docs/07 §7.5）。每场戏由 scene_id 隔离。"""

    def __init__(self) -> None:
        self._scenes: dict[str, Scene] = {}
        self._lock = threading.Lock()

    def create(self, scene_id: str, topic: str, actors: list[str], max_rounds: int) -> Scene:
        sc = Scene(scene_id=scene_id, topic=topic, actors=list(actors), max_rounds=max_rounds)
        with self._lock:
            self._scenes[scene_id] = sc
        return sc

    def join(self, scene_id: str, actor: str) -> dict:
        with self._lock:
            sc = self._scenes.get(scene_id)
            if sc is None or sc.closed:
                return {"ok": False, "reason": "not_found_or_closed"}
            if actor not in sc.actors:
                return {"ok": False, "reason": "not_invited"}
            sc.active.add(actor)
            return {"ok": True, "active": sorted(sc.active)}

    def say(self, scene_id: str, actor: str, speech: str) -> dict:
        with self._lock:
            sc = self._scenes.get(scene_id)
            if sc is None or sc.closed:
                return {"ok": False, "reason": "not_found_or_closed"}
            if actor not in sc.active:
                return {"ok": False, "reason": "not_joined"}
            sc.transcript.append(RoundLine(actor=actor, speech=speech))
            sc.rounds = (len(sc.transcript) // max(1, len(sc.actors))) + 1
            return {"ok": True, "round": sc.rounds, "next_turn": sorted(sc.active)}

    def leave(self, scene_id: str, actor: str) -> dict:
        with self._lock:
            sc = self._scenes.get(scene_id)
            if sc is None:
                return {"ok": False, "reason": "not_found"}
            sc.active.discard(actor)
            # 结束判据：全部离场 -> 直接置关闭（锁内，避免二次 acquire）
            if not sc.active:
                sc.closed = True
                sc.close_reason = "all_left"
            return {"ok": True, "remaining": sorted(sc.active)}

    def close(self, scene_id: str, reason: str) -> dict:
        with self._lock:
            sc = self._scenes.get(scene_id)
            if sc is None:
                return {"ok": False, "reason": "not_found"}
            if not sc.closed:
                sc.closed = True
                sc.close_reason = reason
            return {"ok": True, "closed": True}

    def reached_round_limit(self, scene_id: str) -> bool:
        sc = self._scenes.get(scene_id)
        return bool(sc is not None and sc.rounds >= sc.max_rounds)
