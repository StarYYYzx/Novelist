"""dp-intent：角色欲望字段 + 广播着色（docs/06 §3.1，人物卡 intent/plan）。

覆盖：
- `render_card_line` 事件 prompt 动机着色（intent/plan → 「动机：」行）；无字段不出现（向后兼容）
- 广播可及池 `_one_line` 带欲望证据（供「动机主动」类选角有据可循）
- `Chronicler.record_perspectives` 随视角双写入当刻欲望/计划快照（intent/plan 入记忆）
"""

from __future__ import annotations

import json

import pytest

from novelist.core import broadcast as bc
from novelist.core.chronicler import Chronicler
from novelist.core.director import render_card_line

_SW = {"id": "char:sw", "name": "苏晚", "gender": "female", "status": "active",
       "power": {"level": "炼气四层", "faction": "青云宗"}, "core_traits": ["刚烈", "护短"],
       "intent": "替叶岚洗清冤屈", "plan": "先查青源坊当年卷宗",
       "first_appear": {"vol": 1, "ch": 1}}


def _seed_chars(ws, pid, write):
    write(ws, pid, "bible/characters.json", [
        {"id": "char:yelan", "name": "叶岚", "gender": "male", "status": "active",
         "power": {"level": "炼气三层", "faction": "青云宗"}, "core_traits": ["冷静"],
         "intent": "寻回苏姐的下落", "plan": "夜探藏剑阁",
         "first_appear": {"vol": 1, "ch": 1}, "is_protagonist": True},
        dict(_SW),
    ])


# ---------------------------------------------------------------- 事件 prompt 动机着色

def test_render_card_line_includes_intent():
    line = render_card_line({
        "id": "char:yelan", "name": "叶岚", "gender": "male",
        "core_traits": ["冷静"], "intent": "寻回苏姐的下落", "plan": "夜探藏剑阁"})
    assert "动机" in line
    assert "寻回苏姐的下落" in line or "夜探藏剑阁" in line


def test_render_card_line_no_intent_backward_compat():
    # 无 intent/plan 的旧卡：不出现「动机」段（行结构不受 dp-intent 影响）
    line = render_card_line({"id": "c1", "name": "叶岚", "gender": "male"})
    assert "动机" not in line


def test_render_card_line_prefers_intent_over_plan():
    line = render_card_line(dict(_SW))
    assert "替叶岚洗清冤屈" in line


# ---------------------------------------------------------------- 广播池带欲望证据

def test_broadcast_one_line_includes_intent():
    line = bc._one_line(dict(_SW), names={})
    assert "动机" in line and "替叶岚洗清冤屈" in line


def test_broadcast_one_line_no_intent_backward_compat():
    line = bc._one_line({"id": "char:a", "name": "甲", "core_traits": []}, names={})
    assert "动机" not in line


# ---------------------------------------------------------------- 随视角双写入记忆

def test_record_perspectives_double_writes_intent(ws_factory, stub_llm, write_json):
    ws, pid = ws_factory()
    _seed_chars(ws, pid, write_json)
    c = Chronicler(ws, pid, llm=stub_llm("苏晚 | 焦急 | 想替叶岚寻个公道\n"))
    assert c.record_perspectives("正文……", ["苏晚"], 1, 2, event_index=5) == 1
    data = json.loads(ws.char_history_path(pid, "char:sw").read_text(encoding="utf-8"))
    e = data["entries"][-1]
    assert e["kind"] == "perspective"
    assert e["intent"] == "替叶岚洗清冤屈"   # 卡上欲望随视角快照入记忆
    assert e["plan"] == "先查青源坊当年卷宗"


def test_record_perspectives_no_intent_card_backward_compat(ws_factory, stub_llm, write_json):
    ws, pid = ws_factory()
    # 无 intent 字段的普通卡：视角照常写入，不带 intent 键（不破坏既有数据形状）
    write_json(ws, pid, "bible/characters.json", [
        {"id": "char:yelan", "name": "叶岚", "gender": "male", "status": "active",
         "power": {"level": "炼气三层"}, "first_appear": {"vol": 1, "ch": 1}}])
    c = Chronicler(ws, pid, llm=stub_llm("叶岚 | 冷静 | 什么都没说\n"))
    assert c.record_perspectives("正文……", ["叶岚"], 1, 2, event_index=1) == 1
    data = json.loads(ws.char_history_path(pid, "char:yelan").read_text(encoding="utf-8"))
    assert "intent" not in data["entries"][-1]