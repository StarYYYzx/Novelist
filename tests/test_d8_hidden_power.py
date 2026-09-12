"""D8 回归：隐藏实力（扮猪吃虎）被审校误判「战力越级」block。

背景（2026-09-03 新书 5 章实测）：主角表面练气三层、实际渡劫圆满，越级出手被
Reviewer 判「战力越级/设定矛盾」block×3——爽点机制与一致性引擎冲突。
修复 = 人物卡 `power.hidden_level` 字段（schema 放行）贯通三处：
①book 协议让模型按需给；②审校 brief 渲染并豁免该维度；③导演调度卡注明
越级出手是有意设定。
"""

from __future__ import annotations


from novelist.consistency.reviewer import _bible_brief
from novelist.core.director import render_cards

HIDDEN_CHAR = {"id": "char:li", "name": "李天劫", "gender": "male",
               "core_traits": ["低调", "懒"],
               "power": {"level": "练气三层", "faction": "无",
                         "hidden_level": "渡劫圆满（前世修为，刻意隐藏）"}}


def _mk_project(ws_factory, pid: str, chars: list[dict]):
    ws, proj = ws_factory(pid)
    ws.write_json(ws._abs(f"{proj}/bible/characters.json"), chars)  # noqa: SLF001
    return ws, proj


def test_bible_brief_renders_hidden_power_with_exemption(ws_factory):
    ws, pid = _mk_project(ws_factory, "proj-d8a", [HIDDEN_CHAR])
    brief = _bible_brief(ws, pid)
    assert "实际战力 渡劫圆满（前世修为，刻意隐藏）" in brief
    assert "扮猪吃虎是有意设定" in brief


def test_bible_brief_plain_char_has_no_hidden_note(ws_factory):
    plain = {"id": "char:w", "name": "王傲天", "gender": "male",
             "power": {"level": "练气九层"}}
    ws, pid = _mk_project(ws_factory, "proj-d8b", [plain])
    assert "实际战力" not in _bible_brief(ws, pid)


def test_render_cards_notes_hidden_power():
    out = "\n".join(render_cards([HIDDEN_CHAR]))
    assert "实际战力：渡劫圆满" in out
    assert "越级出手" in out


def test_schema_accepts_power_hidden_level(ws_factory):
    """schema 契约：power.hidden_level 不破坏 bible/characters 校验。"""
    from novelist.core.bible import validate_project
    ws, pid = _mk_project(ws_factory, "proj-d8c", [HIDDEN_CHAR])
    assert validate_project(ws, pid) == []
