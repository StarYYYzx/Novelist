"""分模块审核闸门测试（ADR-024）：开关持久化 / build 拦截 / approve --remember /
revise 定向重生成 / resume 续跑。

**单测绝不真调 LLM**（docs/09 §2.1）——全部 ScriptedProvider。
"""

from __future__ import annotations

import json

from novelist.forge import Blueprint, run_seed
from novelist.forge.engine import build, revise_module
from novelist.forge.nodes import revise_book_section
from novelist.forge.review import (REVIEW_MODULES, diff_section, load_review,
                                   pending_modules, render_review_md,
                                   resolve_pending, set_switch, switch_on)
from novelist.forge.seed import _init_blueprint, _parse_seed_spec
from novelist.providers.fake import ScriptedProvider

from test_m13_forge_f1 import (BOOK_ARTIFACT, SEED_REPLY, _build_script,
                               _chapter_artifact, _node_reply, _volume_artifact)


def _seeded_project(ws_factory, pid: str):
    """建蓝图（不构建），返回 (ws, pid, bp)。"""
    ws, pid = ws_factory(pid)
    spec = _parse_seed_spec(SEED_REPLY)
    bp = _init_blueprint(ws, pid, "brief", spec, _load_pack(), "修仙男频", 2, 2, 1000)
    bp.save(ws, pid)
    return ws, pid, bp


def _load_pack():
    from novelist.forge.genres import load_pack_for

    return load_pack_for("修仙男频")


# ---- 配置与开关 ----
def test_default_switches_all_on(ws_factory):
    ws, pid, _ = _seeded_project(ws_factory, "proj-gate-cfg")
    cfg = load_review(ws, pid)
    assert set(cfg["switches"]) == set(REVIEW_MODULES)
    assert all(cfg["switches"].values()), "默认全开：安全优先"
    assert cfg["pending"] == {}


def test_set_switch_roundtrip(ws_factory):
    ws, pid, _ = _seeded_project(ws_factory, "proj-gate-sw")
    assert switch_on(ws, pid, "characters")
    set_switch(ws, pid, "characters", False)
    assert not switch_on(ws, pid, "characters")
    assert switch_on(ws, pid, "worldview")  # 其他模块不受影响


def test_diff_section_reports_changes():
    old = {"style": {"tense": "过去", "narration": "白描"}, "glossary": []}
    new = {"style": {"tense": "现在", "narration": "白描"}, "glossary": [{"term": "x"}]}
    diffs = diff_section(old, new)
    assert any("style.tense" in d and "过去" in d and "现在" in d for d in diffs)
    assert any("新增" in d and "glossary[0]" in d for d in diffs)


# ---- build 拦截 ----
def test_build_halts_at_book_gate_and_renders_reviews(ws_factory):
    ws, pid, _ = _seeded_project(ws_factory, "proj-gate-halt")
    res = build(ws, pid, provider=ScriptedProvider(_build_script(volumes=2, chapters=2)),
                max_calls=60, deepen=False)  # gate 默认开
    assert res.gate_halted and res.ok, res.warnings  # gate 暂停不算失败
    pending = pending_modules(ws, pid)
    assert set(pending) == {m for m, s in REVIEW_MODULES.items() if s["kind"] == "book"}
    # 评审稿落盘且含内容
    chars_md = ws._abs(f"{pid}/{pending['characters']['file']}").read_text(encoding="utf-8")  # noqa: SLF001
    assert "叶蓝" in chars_md and "审核评审稿" in chars_md
    # 未越卷：book 之后直接暂停
    assert res.volumes_written == 0 and res.chapters_written == 0
    # 已消耗 book 一次调用；再 build（仍有 pending）→ 零新调用直接交还
    used = res.calls_used
    res2 = build(ws, pid, provider=ScriptedProvider([]), max_calls=60,
                 resume=True, deepen=False)
    assert res2.gate_halted and res2.calls_used == used


def test_approve_then_resume_completes(ws_factory):
    ws, pid, _ = _seeded_project(ws_factory, "proj-gate-ok")
    build(ws, pid, provider=ScriptedProvider(_build_script(volumes=2, chapters=2)),
          max_calls=60, deepen=False)
    # 逐模块 approve；文风用 --remember（开关永久关）
    for m in [m for m, s in REVIEW_MODULES.items() if s["kind"] == "book"]:
        resolve_pending(ws, pid, m, decision="approved", remember=(m == "style"))
    assert pending_modules(ws, pid) == {}
    assert not switch_on(ws, pid, "style") and switch_on(ws, pid, "characters")
    # 大纲开关仍开 → resume 会在 volume 闸门再停一次
    r1 = build(ws, pid, provider=ScriptedProvider([
        {"final": _node_reply(_volume_artifact(1))}]),
        max_calls=60, resume=True, deepen=False)
    assert r1.gate_halted and "outline_volume" in r1.pending_review
    # 放行大纲模块后续跑完成
    resolve_pending(ws, pid, "outline_volume", decision="approved")
    set_switch(ws, pid, "outline_volume", False)
    set_switch(ws, pid, "outline_chapter", False)
    r2 = build(ws, pid, provider=ScriptedProvider([
        {"final": _node_reply(_chapter_artifact(1))},
        {"final": _node_reply(_volume_artifact(2))}]),
        max_calls=60, resume=True, deepen=False)
    assert r2.ok and r2.chapters_written >= 1, r2.warnings


# ---- revise ----
def test_revise_book_section_targets_single_module(ws_factory):
    ws, pid, bp = _seeded_project(ws_factory, "proj-gate-rev")
    old_chars = json.loads(json.dumps(bp.data.get("characters")))
    reply = json.dumps({"style": {"tense": "现在", "narration": "第一人称自述",
                                  "glossary": []}}, ensure_ascii=False)
    merged, diffs = revise_book_section(ScriptedProvider([{"final": reply}]),
                                        bp, "style", "改成第一人称")
    assert merged["style"]["narration"] == "第一人称自述"
    assert bp.data["style"]["tense"] == "现在"
    assert bp.data["characters"] == old_chars, "只动目标模块，角色段不受影响"
    assert diffs and any("style" in d for d in diffs)


def test_revise_module_keeps_pending_and_reports(ws_factory):
    ws, pid, _ = _seeded_project(ws_factory, "proj-gate-rev2")
    build(ws, pid, provider=ScriptedProvider(_build_script(volumes=1, chapters=1)),
          max_calls=60, deepen=False)
    assert "style" in pending_modules(ws, pid)
    reply = json.dumps({"style": {"tense": "现在", "narration": "冷峻白描", "glossary": []}},
                       ensure_ascii=False)
    diffs = revise_module(ws, pid, "style", "文风改冷峻", ScriptedProvider([{"final": reply}]))
    assert diffs, "新旧差异必须如实列出"
    cfg = load_review(ws, pid)
    assert "style" in cfg["pending"], "revise 后仍待审"
    assert cfg["pending"]["style"].get("suggestions") == "文风改冷峻"
    # 评审稿已刷新为新内容
    md = ws._abs(f"{pid}/{cfg['pending']['style']['file']}").read_text(encoding="utf-8")  # noqa: SLF001
    assert "冷峻白描" in md


def test_render_review_md_outline_chapter(ws_factory):
    ws, pid, bp = _seeded_project(ws_factory, "proj-gate-render")
    rel = render_review_md(ws, pid, bp, "outline_chapter", vol=1, ch=1)
    md = ws._abs(f"{pid}/{rel}").read_text(encoding="utf-8")  # noqa: SLF001
    assert "第 1 卷第 1 章" in md  # 无细纲文件时也不崩
