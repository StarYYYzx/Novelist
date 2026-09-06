"""商讨轮扩展（2026-09-06 拍板）：pace/romance/opening 三维度 + 世界观深化槽位。

用户拍板：① meta.pace / meta.romance 升 required；② 感情线模式联动线索账本
（单女主/后宫 → 卷纲建议登记 ln:romance，纳入冷却/检查点管束）。
每个新维度必须有消费端——防重蹈 threads_involved"填了没人消费"的覆辙。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from novelist.core import lines as L
from novelist.forge.nodes import (_OPENING_RULE, _PACE_VOLUME_HINT,
                                  _ROMANCE_RULE, _chapter_meta_rules,
                                  _lines_block_for_volume, _pace_section)
from novelist.forge.slots import default_slots
from novelist.storage.workspace import Workspace

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- schema

def _bp_doc(**extra):
    doc = {
        "rev": 1,
        "provenance": {},
        "meta": {"title": "t", "genre": "修仙", "logline": "l",
                 "scale": {"volumes": 3, "chapters_per_volume": 20,
                           "target_words_per_chapter": 2400}},
    }
    doc["meta"].update(extra)
    return doc


def test_schema_accepts_new_meta_dims():
    import jsonschema

    schema = json.loads(
        (ROOT / "schemas/forge/blueprint.schema.json").read_text(encoding="utf-8"))
    v = jsonschema.Draft202012Validator(schema)
    doc = _bp_doc(pace="苟住发育", romance="单女主", opening="慢热铺垫")
    assert not list(v.iter_errors(doc))
    # 旧蓝图（无新字段）仍合法——schema 不 required，访谈层管必答
    assert not list(v.iter_errors(_bp_doc()))
    # 非法枚举拒绝
    assert list(v.iter_errors(_bp_doc(pace="无敌流")))
    assert list(v.iter_errors(_bp_doc(romance="种马")))


def test_schema_worldview_and_character_fields():
    import jsonschema

    schema = json.loads(
        (ROOT / "schemas/forge/blueprint.schema.json").read_text(encoding="utf-8"))
    v = jsonschema.Draft202012Validator(schema)
    doc = _bp_doc()
    doc["worldview"] = {"power_system": {"levels": ["练气"], "mechanic": "m",
                                         "ceiling": "化神大圆满"},
                        "map": "青山镇→青云宗→中州"}
    doc["characters"] = [{"id": "char:a", "name": "甲", "role": "protagonist",
                          "flaw": "不信任何人"}]
    assert not list(v.iter_errors(doc))


# ---------------------------------------------------------------- 槽位

def test_slots_new_interview_dimensions():
    by_key = {s.key: s for s in default_slots()}
    pace = by_key["meta.pace"]
    assert pace.level == "required" and pace.group == 1
    assert "苟住发育" in pace.enum
    romance = by_key["meta.romance"]
    assert romance.level == "required" and romance.group == 4
    assert set(romance.enum) == {"无CP", "单女主", "多女主后宫", "副线淡化"}
    opening = by_key["meta.opening"]
    assert opening.level == "recommended" and set(opening.enum) == set(_OPENING_RULE)
    # 世界观深化 + 人物缺陷 + 感情线对象排在模式之后
    for key in ("worldview.power_system.ceiling", "worldview.map",
                "characters[role:protagonist].flaw"):
        assert key in by_key and by_key[key].level == "recommended"
    li = by_key["characters[role:love_interest].name"]
    assert "无CP" in li.ask      # 提示可跳过


def test_new_required_slots_flow_through_detect_gaps():
    """新增 required 槽位必须被缺口检测覆盖（否则访谈不会问）。"""
    from novelist.forge.slots import Blueprint, detect_gaps

    bp = Blueprint.blank({"title": "t", "genre": "修仙", "logline": "l",
                          "scale": {"volumes": 1, "chapters_per_volume": 1,
                                    "target_words_per_chapter": 100}})
    gaps = {g.slot.key for g in detect_gaps(bp) if g.slot.level == "required"}
    assert {"meta.pace", "meta.romance"} <= gaps
    bp.upsert("characters", {"id": "char:a", "name": "甲", "gender": "male",
                             "role": "protagonist"})
    for slot in default_slots():
        if slot.level == "required" and not slot.key.startswith("characters["):
            bp.set(slot.key, "稳健推进" if slot.key == "meta.pace"
                   else ("副线淡化" if slot.key == "meta.romance"
                         else _fill_for(slot.key)))
            bp.set_provenance(slot.key, "user", 1.0)
    assert all(g.slot.level == "recommended" for g in detect_gaps(bp))


def _fill_for(key: str):
    """按 key 给个最小可填值（detect_gaps 判定的是值本身，非仅 provenance）。"""
    return {
        "meta.genre": "修仙",
        "worldview.power_system.mechanic": "共享修炼",
        "meta.logline": "l",
        "meta.scale": {"volumes": 1, "chapters_per_volume": 1,
                       "target_words_per_chapter": 100},
        "worldview.power_system.levels": ["练气"],
        "style.tone": "热血激昂",
        "style.pov": "第三人称限知（主角视角）",
    }.get(key, "x")


# ---------------------------------------------------------------- 注入 helpers

def test_pace_section_per_mode():
    assert "守住既有成果即胜" in _pace_section({"pace": "苟住发育"})
    assert "30%-40%" in _pace_section({"pace": "先抑后扬"})
    assert _pace_section({}) == ""
    assert _pace_section({"pace": "未知"}) == ""
    assert set(_PACE_VOLUME_HINT) == {"平推爽文", "苟住发育", "先抑后扬", "稳健推进"}


def test_chapter_meta_rules_opening_and_romance():
    # 开篇节奏只作用于前三章
    out = _chapter_meta_rules({"opening": "金手指速觉醒", "romance": "无CP"}, 3)
    assert "第 3 章结束前" in out and "不得生成感情戏" in out
    assert "开篇节奏" not in _chapter_meta_rules({"opening": "金手指速觉醒"}, 4)
    # 感情线约束所有章生效
    out2 = _chapter_meta_rules({"romance": "多女主后宫"}, 9)
    assert "不得长期遗忘" in out2
    assert _chapter_meta_rules({}, 1) == ""
    assert set(_ROMANCE_RULE) == {"无CP", "单女主", "多女主后宫", "副线淡化"}


# ---------------------------------------------------------------- 感情线联动账本

def _ws(tmp_path):
    ws = Workspace(root=tmp_path / "ws")
    ws.create_project("proj-ln")
    return ws


class _Ctx:
    def __init__(self, ws, bp=None):
        self.ws = ws
        self.project_id = "proj-ln"
        self.bp = bp


def test_volume_lines_block_suggests_romance_line(tmp_path):
    from novelist.forge.state import Blueprint

    ws = _ws(tmp_path)
    bp = Blueprint.blank({"title": "t", "genre": "修仙", "logline": "l",
                          "scale": {"volumes": 2, "chapters_per_volume": 10,
                                    "target_words_per_chapter": 2000}})
    bp.set("meta.romance", "单女主")
    # 账本为空（book 骨架没给线）→ 仍给建议
    blk = _lines_block_for_volume(_Ctx(ws, bp), 1)
    assert "感情线登记建议" in blk and "ln:romance" in blk
    # 无 romance 模式（旧蓝图/无CP）→ 无建议
    bp.set("meta.romance", "无CP")
    assert "感情线登记建议" not in _lines_block_for_volume(_Ctx(ws, bp), 1)
    # 账本已有感情线（id 或 desc 命中）→ 不重复建议
    bp.set("meta.romance", "多女主后宫")
    L.save_lines(ws, "proj-ln", [L.new_line("ln:yun", "云溪与主角的感情线", kind="subplot")])
    assert "感情线登记建议" not in _lines_block_for_volume(_Ctx(ws, bp), 1)
    # 无 romance 模式但账本空 → 无线块也无建议（返回空串，不注垃圾）
    bp2 = Blueprint.blank({"title": "t", "genre": "修仙", "logline": "l",
                           "scale": {"volumes": 2, "chapters_per_volume": 10,
                                     "target_words_per_chapter": 2000}})
    ws2 = Workspace(root=tmp_path / "ws2")
    ws2.create_project("proj-ln")
    assert _lines_block_for_volume(_Ctx(ws2, bp2), 1) == ""


def test_volume_lines_block_still_works_without_bp():
    """ctx.bp 为 None（旧测试桩/降级路径）不炸。"""
    ws = _ws(Path(_ws_tmp := __import__("tempfile").mkdtemp()))
    L.save_lines(ws, "proj-ln", [L.new_line("ln:m", "主线", kind="main",
                                            target={"vol": 1, "note": "n"})])
    blk = _lines_block_for_volume(_Ctx(ws, None), 1)
    assert "ln:m" in blk
