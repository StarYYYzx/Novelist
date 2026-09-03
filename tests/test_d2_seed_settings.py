"""D2 回归：seed 模式 forge build 不产 settings → V3（≥5）必挡、检索空转。

修复 = sync_bible 在蓝图 settings 段与磁盘文件双空时，零 LLM 从蓝图实体
（worldview/characters/locations/items/skills）合成种子设定卡——不发明新设定，
只把应然事实切成检索知识单元；enrich 增量（后写文件/蓝图）不被覆盖。
"""

from __future__ import annotations

import json

from novelist.core.bible import validate_project
from novelist.forge.nodes import sync_bible, synthesize_seed_settings
from novelist.forge.state import Blueprint

SEED_META = {"title": "t", "genre": "修仙", "logline": "x",
             "scale": {"volumes": 1, "chapters_per_volume": 1, "target_words_per_chapter": 100}}


def _mk_bp() -> Blueprint:
    bp = Blueprint.blank(dict(SEED_META))
    bp.data["worldview"] = {"name": "蓝星复苏", "power_system": {"mechanic": "灵气复苏后修炼",
                                                                "levels": ["练气", "筑基"]}}
    bp.upsert("characters", {"id": "char:li", "name": "李天劫", "gender": "male",
                             "background": "渡劫圆满回归蓝星",
                             "power": {"level": "练气三层", "hidden_level": "渡劫圆满"},
                             "first_appear": {"vol": 1, "ch": 1}})
    bp.upsert("characters", {"id": "char:z", "name": "张胖子"})
    bp.upsert("items", {"id": "item:1", "name": "护身玉符", "type": "artifact", "desc": "护主挡劫"})
    return bp


def test_synth_cards_shape_and_content():
    cards = synthesize_seed_settings(_mk_bp())
    assert len(cards) == 4  # world 1 + characters 2 + items 1（实体数即卡数，V3 达标看实体量）
    ids = [c["id"] for c in cards]
    assert len(ids) == len(set(ids)), "id 必须唯一"
    assert all(c["id"].startswith("set:") for c in cards)
    assert all(c["keywords"] for c in cards)  # schema minItems 1
    assert all(c["revealed"] is False for c in cards)
    li = next(c for c in cards if "李天劫" in c["keywords"])
    assert "渡劫圆满回归蓝星" in li["text"] and "隐藏实力 渡劫圆满" in li["text"]
    assert li["first_ch"] == 1


def test_sync_bible_writes_seed_settings_when_both_empty(ws_factory):
    ws, pid = ws_factory("proj-d2a")
    bp = _mk_bp()
    written = sync_bible(ws, pid, bp)
    assert "bible/settings.json" in written
    cards = json.loads(ws._abs(f"{pid}/bible/settings.json").read_text(encoding="utf-8"))  # noqa: SLF001
    assert len(cards) == len(synthesize_seed_settings(bp))
    assert validate_project(ws, pid) == []  # 契约全过（settings 过 schema）


def test_sync_bible_never_clobbers_enrich_increment(ws_factory):
    """蓝图空 + 文件已有内容（enrich 增量）→ 不重写。"""
    ws, pid = ws_factory("proj-d2b")
    bp = _mk_bp()
    sync_bible(ws, pid, bp)
    # 模拟 enrich 往文件追加一张卡
    p = ws._abs(f"{pid}/bible/settings.json")  # noqa: SLF001
    cards = json.loads(p.read_text(encoding="utf-8"))
    cards.append({"id": "set:enrich:1", "keywords": ["灵潮"], "text": "灵潮夜诡异出没"})
    p.write_text(json.dumps(cards, ensure_ascii=False), encoding="utf-8")
    # bp.settings 仍为空 → 再跑 sync_bible 不覆盖
    sync_bible(ws, pid, bp)
    after = json.loads(p.read_text(encoding="utf-8"))
    assert any(c["id"] == "set:enrich:1" for c in after)


def test_bp_settings_takes_precedence(ws_factory):
    """蓝图 settings 段非空（LLM 产物/enrich 回写）→ 不合成，照原白名单落盘。"""
    ws, pid = ws_factory("proj-d2c")
    bp = _mk_bp()
    bp.upsert("settings", {"id": "set:bp:1", "keywords": ["灵潮"], "text": "蓝图自带"})
    sync_bible(ws, pid, bp)
    cards = json.loads(ws._abs(f"{pid}/bible/settings.json").read_text(encoding="utf-8"))  # noqa: SLF001
    assert [c["id"] for c in cards] == ["set:bp:1"]
