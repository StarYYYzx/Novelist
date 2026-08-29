"""围读会相关工具（docs/05 §4.1 / docs/07 §7.5，ADR-014）：join_scene/say_line/leave_scene。"""

from __future__ import annotations

from ..core.tools import Tool


def make_scene_tools(scene_bus) -> list[Tool]:
    """构造围读会工具（safe 级，绑定给定 SceneBus）。"""

    def _join(session, params, budget=None):
        return scene_bus.join(params["scene_id"], session.actor if not session.actor_char_id else session.actor_char_id)

    def _say(session, params, budget=None):
        actor = session.actor_char_id or session.actor
        return scene_bus.say(params["scene_id"], actor, params["speech"])

    def _leave(session, params, budget=None):
        actor = session.actor_char_id or session.actor
        return scene_bus.leave(params["scene_id"], actor)

    return [
        Tool("join_scene", "加入围读会场景", "safe", _join, {"scene_id": {"type": "string"}}),
        Tool(
            "say_line",
            "在围读会场景说话（广播给同场景演员）",
            "safe",
            _say,
            {"scene_id": {"type": "string"}, "speech": {"type": "string"}},
        ),
        Tool("leave_scene", "离开围读会场景", "safe", _leave, {"scene_id": {"type": "string"}}),
    ]
