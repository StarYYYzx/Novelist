"""M3l F0 骨架测试（docs/10 §13 / docs/08 F0）：state + slots + Genre Pack + forge show。

覆盖（确定性，零 LLM）：
- blueprint schema 自身校验（V1：中间态可校验）
- Genre Pack 装载（id/别名/兜底/自身 schema）
- slots 缺口检测（未填/低置信/角色伪路径）+ 分组
- Blueprint 读写往返 / provenance 保护 / upsert 幂等 / transcript
- ForgeState 读写 + CLI `forge show` 集成
"""

from __future__ import annotations

import json
import re

import pytest
from click.testing import CliRunner

from novelist.cli import cli
from novelist.forge import Blueprint, ForgeState, load_pack, load_pack_for, list_packs
from novelist.forge.slots import detect_gaps, group_slots, slots_for_genre
from novelist.storage.models import SchemaError, SchemaRegistry


# ---- blueprint schema 自身校验 ----
def test_blueprint_schema_rejects_missing_required():
    reg = SchemaRegistry()
    with pytest.raises(SchemaError):
        reg.validate("forge/blueprint", {"rev": 1})  # 缺 provenance/meta


def test_blueprint_schema_rejects_bad_provenance_src():
    data = Blueprint.blank({"title": "t", "genre": "修仙", "logline": "x",
                            "scale": {"volumes": 1, "chapters_per_volume": 1,
                                      "target_words_per_chapter": 100}}).data
    data["provenance"]["meta.logline"] = {"src": "alien", "confidence": 1.0}
    with pytest.raises(SchemaError):
        SchemaRegistry().validate("forge/blueprint", data)


def test_blueprint_schema_accepts_id_indexed_provenance_key():
    data = Blueprint.blank({"title": "t", "genre": "修仙", "logline": "x",
                            "scale": {"volumes": 1, "chapters_per_volume": 1,
                                      "target_words_per_chapter": 100}}).data
    data["provenance"]["characters[char:luchen].name"] = {"src": "user", "confidence": 1.0}
    SchemaRegistry().validate("forge/blueprint", data)  # 不抛即通过（冒号键合法）


def test_blank_blueprint_self_valid():
    bp = Blueprint.blank({"title": "t", "genre": "修仙", "logline": "x",
                          "scale": {"volumes": 1, "chapters_per_volume": 1,
                                    "target_words_per_chapter": 100}})
    bp.validate()  # 不抛即通过


# ---- Genre Pack 装载 ----
def test_genre_pack_list_and_id_load():
    packs = list_packs()
    assert "修仙男频" in packs and "通用" in packs
    pack = load_pack("修仙男频")
    assert pack["power_system"]["levels"][0] == "练气"
    assert pack["default_style"]["target_words_per_chapter"] == 2400


def test_genre_pack_self_validates():
    # 装载即校验（genres.schema.json）；schema 缺 required id/genre 的包会被拒
    for pid in list_packs():
        p = load_pack(pid)
        assert p["id"] == pid


def test_genre_pack_alias_and_fallback():
    assert load_pack_for("仙侠")["id"] == "修仙男频"
    assert load_pack_for("修真")["id"] == "修仙男频"
    assert load_pack_for("西方奇幻")["id"] == "通用"  # 库外兜底
    assert load_pack_for("")["id"] == "通用"
    assert load_pack_for("修仙男频")["id"] == "修仙男频"


def test_genre_pack_unavailable_states_present():
    # 决策 29：unavailable_states 放 Genre Pack，通用包给基础词表
    for pid in ("修仙男频", "通用"):
        states = load_pack(pid)["extract_lexicon"]["unavailable_states"]
        assert "闭关" in states and "渡劫" in states


def test_genre_pack_slot_overrides():
    pack = load_pack("修仙男频")
    slots = slots_for_genre(pack)
    by_key = {s.key: s for s in slots}
    levels = by_key["worldview.power_system.levels"]
    assert levels.default == "练气-筑基-金丹-元婴-化神"  # pack.slots 覆盖默认
    tone = by_key["style.tone"]
    assert "热血激昂" in tone.enum


# ---- slots：缺口检测 ----
def _full_bp():
    bp = Blueprint.blank({"title": "t", "genre": "修仙", "logline": "五五开系统",
                          "scale": {"volumes": 3, "chapters_per_volume": 20,
                                    "target_words_per_chapter": 2400}})
    bp.upsert("characters", {"id": "char:yelan", "name": "叶蓝", "gender": "male",
                             "role": "protagonist", "core_traits": ["坚韧", "狡猾", "厚脸皮"]})
    bp.set("worldview.power_system", {"levels": ["练气", "筑基"], "mechanic": "绑定他人共享修炼"})
    bp.set("worldview.name", "青云界")
    bp.set("worldview.factions", ["青云宗"])
    bp.set("worldview.rules", ["铁律一"])
    bp.set("style", {"tone": ["热血激昂"], "pov": "第三人称限知（主角视角）", "tense": "过去"})
    bp.set("meta.endgame", "飞升")
    bp.section("threads").append({"id": "pt:yupai", "desc": "玉牌之谜"})
    bp.upsert("characters", {"id": "char:hepao", "name": "贺袍", "role": "rival"})
    return bp


def test_detect_gaps_empty_blueprint_reports_all_required():
    bp = Blueprint.blank({"title": "t", "genre": "修仙",
                          "scale": {"volumes": 1, "chapters_per_volume": 1,
                                    "target_words_per_chapter": 100}})
    gaps = detect_gaps(bp)
    reasons = {g.slot.key for g in gaps}
    assert "characters[role:protagonist].name" in reasons
    assert "worldview.power_system.mechanic" in reasons
    assert "style.tone" in reasons
    # 排序：required 未填在最前
    assert all(g.slot.level == "required" for g in gaps[:6])


def test_detect_gaps_full_blueprint_no_required_gaps():
    bp = _full_bp()
    for slot in slots_for_genre(load_pack("修仙男频")):
        if slot.level == "required":
            bp.set_provenance(slot.key, "user", 1.0)
    gaps = detect_gaps(bp)
    assert all(g.slot.level == "recommended" for g in gaps)


def test_detect_gaps_low_confidence_reported():
    bp = _full_bp()
    for slot in slots_for_genre(None):
        if slot.level == "required":
            bp.set_provenance(slot.key, "user", 1.0)
    # 把 style.tone 降为 llm 低置信 → 出现 low_confidence 缺口
    bp.set_provenance("style.tone", "llm", 0.3)
    gaps = detect_gaps(bp)
    low = [g for g in gaps if g.slot.key == "style.tone"]
    assert low and low[0].reason == "low_confidence"


def test_detect_gaps_role_pseudo_path_resolves():
    # characters[role:protagonist].name 按角色匹配，无下标依赖
    bp = _full_bp()
    assert bp.filled("characters[role:protagonist].name") is False  # filled() 不认识伪路径
    # 但 detect_gaps 认识
    gaps = {g.slot.key: g for g in detect_gaps(bp)}
    assert "characters[role:protagonist].name" not in gaps or gaps["characters[role:protagonist].name"].reason != "unfilled"


def test_group_slots_rounds():
    rounds = group_slots(slots_for_genre(load_pack("修仙男频")))
    names = [name for name, _ in rounds]
    # H4 修复（2026-09-05）：超出 per_round 的槽位顺延"（续）"轮，不再静默丢弃
    base = [n for n in names if not n.endswith("（续）")]
    assert base == ["书级必填", "世界与规则", "文风与叙事", "人物与伏笔"]
    assert any(n.endswith("（续）") for n in names)  # 溢出槽位有续轮
    for _, items in rounds:
        assert len(items) <= 4


# ---- state：读写 / provenance / upsert / transcript ----
def test_blueprint_roundtrip_and_provenance(ws_factory):
    ws, pid = ws_factory()
    bp = Blueprint.blank({"title": "t", "genre": "修仙", "logline": "五五开系统",
                          "scale": {"volumes": 1, "chapters_per_volume": 1,
                                    "target_words_per_chapter": 100}})
    bp.set_provenance("meta.title", "user", 1.0)
    bp.set_provenance("worldview.name", "llm", 0.4, evidence="seed")
    bp.save(ws, pid)
    bp2 = Blueprint.load(ws, pid)
    assert bp2.data["rev"] == 1
    assert bp2.is_protected("meta.title")
    assert not bp2.is_protected("worldview.name")
    assert bp2.low_confidence_paths() == ["worldview.name"]
    assert bp2.provenance_summary()["user"] == 1


def test_blueprint_upsert_idempotent(ws_factory):
    ws, pid = ws_factory()
    bp = Blueprint.blank({"title": "t", "genre": "修仙", "logline": "五五开系统",
                          "scale": {"volumes": 1, "chapters_per_volume": 1,
                                    "target_words_per_chapter": 100}})
    bp.upsert("characters", {"id": "char:a", "name": "A", "role": "protagonist"})
    bp.upsert("characters", {"id": "char:a", "name": "A2", "role": "protagonist"})  # 覆盖
    bp.upsert("characters", {"id": "char:b", "name": "B", "role": "rival"})
    assert len(bp.section("characters")) == 2  # 不产生重复条目
    assert bp.find_by_id("characters", "char:a")["name"] == "A2"
    bp.save(ws, pid)
    bp2 = Blueprint.load(ws, pid)
    assert len(bp2.section("characters")) == 2


def test_forge_state_roundtrip_and_project_schema(ws_factory):
    ws, pid = ws_factory()
    st = ForgeState.load(ws, pid)
    assert st.stage == "slots" and st.calls_used == 0
    st.mode, st.interaction, st.stage, st.calls_used = "seed", "auto", "build", 12
    st.save(ws, pid)
    data = json.loads((ws.project_json_path(pid)).read_text(encoding="utf-8"))
    SchemaRegistry().validate("project", data)  # forge 段过 project schema
    st2 = ForgeState.load(ws, pid)
    assert st2.stage == "build" and st2.calls_used == 12
    st2.touch_stage(ws, pid, "done")
    assert ForgeState.load(ws, pid).stage == "done"


def test_transcript_append_and_read(ws_factory):
    ws, pid = ws_factory()
    from novelist.forge import append_transcript, read_transcript
    append_transcript(ws, pid, "ask", key="style.tone")
    append_transcript(ws, pid, "answer", value="热血激昂")
    rows = read_transcript(ws, pid)
    assert len(rows) == 2
    assert rows[0]["event"] == "ask" and rows[0]["key"] == "style.tone"
    assert rows[1]["event"] == "answer" and rows[1]["value"] == "热血激昂"


# ---- CLI forge show 集成 ----
def _seed_blueprint(ws, pid, **kw):
    bp = Blueprint.blank({
        "title": kw.get("title", "测试书"), "genre": "修仙", "template": "修仙男频",
        "logline": kw.get("logline", "五五开系统"),
        "scale": {"volumes": 3, "chapters_per_volume": 20, "target_words_per_chapter": 2400},
    })
    bp.set_provenance("meta.logline", "user", 1.0)
    bp.save(ws, pid)
    return bp


def test_cli_forge_show(ws_factory, tmp_path):
    ws, pid = ws_factory()
    _seed_blueprint(ws, pid)
    result = CliRunner().invoke(cli, ["forge", "show", str(tmp_path)])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "== proj-test ==" in out
    assert "蓝图 rev=1" in out
    assert "测试书" in out and "修仙男频" in out
    assert "缺口: 18 处" in out  # 空蓝图 18 缺口（09-04 增补 style.narration 槽位）
    assert "来源: user=1" in out


def test_cli_forge_show_no_blueprint(ws_factory, tmp_path):
    ws, pid = ws_factory()
    result = CliRunner().invoke(cli, ["forge", "show", str(tmp_path)])
    assert result.exit_code != 0
    assert "尚无蓝图" in result.output


def test_cli_forge_show_multiple_projects(tmp_path):
    from novelist.storage.checkpoint import Checkpoint
    from novelist.storage.workspace import Workspace
    ws = Workspace(root=str(tmp_path))
    for pid in ("proj-a", "proj-b"):
        ws.create_project(pid)
        Checkpoint(ws).save(pid, {"id": pid, "title": pid, "pipeline_state": "立项"})
    result = CliRunner().invoke(cli, ["forge", "show", str(tmp_path)])
    assert result.exit_code != 0
    assert "multiple projects" in result.output
