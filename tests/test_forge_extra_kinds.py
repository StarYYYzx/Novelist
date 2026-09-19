"""类型包声明式扩展节点测试（2026-09-19 拍板，档 2 后半）。

白名单机制：genre pack JSON 声明 `extra_node_kinds`（kind/label/section/hint）；
模型只能产出包内声明的 kind，白名单外/段非法一律硬拒。
单测绝不真调 LLM（docs/09 §2.1）——ScriptedProvider。
"""

from __future__ import annotations

import json

import pytest

from novelist.forge.nodes import NodeContext, run_node
from novelist.forge.state import Blueprint
from novelist.providers.fake import ScriptedProvider


def _pack_with_extra():
    from novelist.forge.genres import load_pack_for

    pack = dict(load_pack_for("修仙男频"))
    pack["extra_node_kinds"] = [
        {"kind": "secret_realm", "label": "秘境规则书", "section": "settings",
         "hint": "设计本书核心秘境的进入条件/规则/代价。"},
    ]
    return pack


def _ctx(ws, pid, bp, pack, script):
    return NodeContext(ws=ws, project_id=pid, bp=bp, pack=pack,
                       provider=ScriptedProvider(script))


def test_extra_node_runs_and_upserts(ws_factory):
    """声明过的扩展 kind：走通用 prompt + 落声明的段。"""
    ws, pid = ws_factory("proj-extra-ok")
    bp = Blueprint.blank()
    bp.data["meta"] = {"title": "t", "genre": "修仙", "logline": "x",
                       "scale": {"volumes": 1, "chapters_per_volume": 2,
                                 "target_words_per_chapter": 100}}
    ctx = _ctx(ws, pid, bp, _pack_with_extra(),
               [{"final": json.dumps({"artifact": {"name": "归墟秘境",
                                                   "rule": "入境者须以一段记忆为代价"}},
                                     ensure_ascii=False)}])
    res = run_node(ctx, "secret_realm")
    assert res.ok and res.decide == "done"
    rows = bp.section("settings")
    # id 按目标段 schema 口径生成（settings → set: 前缀，中文名走稳定 hash slug）
    assert any(str(r.get("id", "")).startswith("set:") and r.get("name") == "归墟秘境"
               and r.get("text") for r in rows), rows
    # provenance 留痕
    prov = bp.data.get("provenance") or {}
    assert any(k.startswith("settings[set:") for k in prov)


def test_extra_node_undeclared_kind_rejected(ws_factory):
    """白名单外的 kind：硬拒（与固定 12 种之外的 unknown kind 同一口径）。"""
    ws, pid = ws_factory("proj-extra-no")
    bp = Blueprint.blank()
    bp.data["meta"] = {"title": "t", "genre": "修仙", "logline": "x",
                       "scale": {"volumes": 1, "chapters_per_volume": 2,
                                 "target_words_per_chapter": 100}}
    ctx = _ctx(ws, pid, bp, _pack_with_extra(), [{"final": "{}"}])
    with pytest.raises(ValueError, match="unknown node kind"):
        run_node(ctx, "free_for_all")


def test_extra_node_bad_section_rejected(ws_factory):
    """声明写非法段（如 worldview）→ 硬拒，扩展节点只许写 list 段。"""
    ws, pid = ws_factory("proj-extra-badsec")
    bp = Blueprint.blank()
    bp.data["meta"] = {"title": "t", "genre": "修仙", "logline": "x",
                       "scale": {"volumes": 1, "chapters_per_volume": 2,
                                 "target_words_per_chapter": 100}}
    pack = dict(_pack_with_extra())
    pack["extra_node_kinds"] = [{"kind": "evil", "label": "x", "section": "worldview"}]
    ctx = _ctx(ws, pid, bp, pack, [{"final": "{}"}])
    with pytest.raises(ValueError, match="unknown node kind"):
        run_node(ctx, "evil")


def test_extra_node_empty_artifact_rejected(ws_factory):
    ws, pid = ws_factory("proj-extra-empty")
    bp = Blueprint.blank()
    bp.data["meta"] = {"title": "t", "genre": "修仙", "logline": "x",
                       "scale": {"volumes": 1, "chapters_per_volume": 2,
                                 "target_words_per_chapter": 100}}
    ctx = _ctx(ws, pid, bp, _pack_with_extra(),
               [{"final": json.dumps({"artifact": None}, ensure_ascii=False)}])
    with pytest.raises(ValueError, match="artifact"):
        run_node(ctx, "secret_realm")


def test_deepen_build_runs_extra_kind(ws_factory, capsys):
    """引擎级：deepen 构建在四个固定旁支后跑扩展节点，产物进 settings。"""
    from test_m17_forge_f4 import _deepen_script, _init_bp, _reply

    ws, pid = ws_factory("proj-extra-e2e")
    _init_bp(ws, pid)
    script = _deepen_script(chapters=2)
    # 扩展节点在 thread_set 之后、volume 之前插入一次调用
    # 脚本顺序：book, worldview, system×2, character_group, style, thread_set, [extra], v1, ch×2, v2
    script.insert(7, _reply({"name": "归墟秘境", "rule": "以记忆为代价"}))
    from novelist.forge.engine import build

    r = build(ws, pid, provider=ScriptedProvider(script), max_calls=60, gate=False,
              pack=_pack_with_extra())
    assert r.ok, r.warnings
    settings = json.loads(ws.bible_path(pid, "settings").read_text(encoding="utf-8"))
    # sync_bible 的 settings 白名单只留 id/keywords/text/revealed/first_ch（无 name）——
    # 扩展条目的名字进 keywords、内容进 text
    assert any("归墟秘境" in (s.get("keywords") or []) for s in settings), settings
    assert any("代价" in (s.get("text") or "") for s in settings), settings
    capsys.readouterr()
