"""分模块审核闸门测试（ADR-024）：开关持久化 / build 拦截 / approve --remember /
revise 定向重生成 / resume 续跑。

**单测绝不真调 LLM**（docs/09 §2.1）——全部 ScriptedProvider。
"""

from __future__ import annotations

import json

from novelist.forge.engine import build, revise_module
from novelist.forge.nodes import revise_book_section
from novelist.forge.review import (REVIEW_MODULES, approve_all, diff_section,
                                   load_review, pending_modules, render_review_md,
                                   resolve_all_pending, resolve_pending,
                                   set_all_switches, set_switch, switch_on)
from novelist.forge.seed import _init_blueprint, _parse_seed_spec
from novelist.providers.fake import ScriptedProvider

from test_m13_forge_f1 import (SEED_REPLY, _build_script,
                         _event_stream_artifact,
                               _node_reply, _volume_artifact)


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


# ---- UX-1（2026-09-19）：批量批准与 approve-all 放行机制 ----

def _pend(ws, pid, *mods):
    """造 pending：直接写配置（等价于 build 闸门挂起，但不动 LLM）。"""
    cfg = load_review(ws, pid)
    for m in mods:
        cfg["pending"][m] = {"file": f"workspace/forge/reviews/{m}.md",
                             "node": REVIEW_MODULES[m]["kind"], "at": "2026-09-19 10:00:00"}
    from novelist.forge.review import save_review
    save_review(ws, pid, cfg)


def test_approve_all_pending_clears_everything(ws_factory):
    """`/approve all`：批量批准全部待审，逐模块留痕。"""
    ws, pid, _ = _seeded_project(ws_factory, "proj-gate-all")
    _pend(ws, pid, "worldview", "characters", "style")
    mods = resolve_all_pending(ws, pid)
    assert mods == ["characters", "style", "worldview"]
    assert not pending_modules(ws, pid)
    hist = load_review(ws, pid)["history"]
    assert len(hist) == 3 and all(h["note"] == "approve all" for h in hist)
    # 未 remember → 开关仍开（下次构建还会审）
    assert switch_on(ws, pid, "worldview")


def test_approve_all_pending_remember_closes_switches(ws_factory):
    ws, pid, _ = _seeded_project(ws_factory, "proj-gate-allrm")
    _pend(ws, pid, "worldview", "style")
    resolve_all_pending(ws, pid, remember=True)
    assert not switch_on(ws, pid, "worldview") and not switch_on(ws, pid, "style")
    assert switch_on(ws, pid, "characters")  # 未涉及的模块不动


def test_approve_all_target_module_closes_switch(ws_factory):
    """`/approve-all <模块>`：批准待审 + 永久关开关（编码 agent 的 allow-always）。"""
    ws, pid, _ = _seeded_project(ws_factory, "proj-gate-aamod")
    _pend(ws, pid, "worldview")
    msg = approve_all(ws, pid, "worldview")
    assert "worldview" in msg and "不再审核" in msg
    assert not pending_modules(ws, pid)
    assert not switch_on(ws, pid, "worldview")
    hist = load_review(ws, pid)["history"]
    assert hist[-1]["decision"] == "approve-all" and hist[-1]["remember"]


def test_approve_all_all_without_pending_still_closes_all(ws_factory):
    """`/approve-all all` 在无 pending 时也要把开关全关（用户意图=以后都别审）。"""
    ws, pid, _ = _seeded_project(ws_factory, "proj-gate-aaall")
    msg = approve_all(ws, pid, "all")
    assert "后续构建不再人工审核" in msg
    cfg = load_review(ws, pid)
    assert not any(cfg["switches"].values()), "全部开关应已关闭"
    assert any(h["decision"] == "approve-all" for h in cfg["history"])


def test_approve_all_off_resumes_review(ws_factory):
    """`/approve-all <模块|all> off`：重新打开开关恢复审核，pending 不动。"""
    ws, pid, _ = _seeded_project(ws_factory, "proj-gate-aaoff")
    approve_all(ws, pid, "all")
    _pend(ws, pid, "style")
    msg = approve_all(ws, pid, "style", off=True)
    assert "恢复审核" in msg
    assert switch_on(ws, pid, "style")
    assert not switch_on(ws, pid, "worldview")  # 其他模块仍关
    assert "style" in pending_modules(ws, pid)  # off 不动 pending
    hist = load_review(ws, pid)["history"]
    assert hist[-1]["decision"] == "review-resume"


def test_approve_all_unknown_target_rejected(ws_factory):
    import pytest

    ws, pid, _ = _seeded_project(ws_factory, "proj-gate-aabad")
    with pytest.raises(ValueError, match="未知模块"):
        approve_all(ws, pid, "nope")


def test_approve_all_all_unblocks_build(ws_factory):
    """端到端：approve-all all 之后 build 不再被闸门拦（开关全关 → _gated 为空）。"""
    ws, pid, _ = _seeded_project(ws_factory, "proj-gate-aabuild")
    approve_all(ws, pid, "all")
    res = build(ws, pid, provider=ScriptedProvider(_build_script(volumes=2, chapters=2)),
                max_calls=60, deepen=False)
    assert not res.gate_halted, res.warnings
    assert not pending_modules(ws, pid)


def test_set_all_switches_roundtrip(ws_factory):
    ws, pid, _ = _seeded_project(ws_factory, "proj-gate-swall")
    set_all_switches(ws, pid, False)
    assert not any(load_review(ws, pid)["switches"].values())
    set_all_switches(ws, pid, True)
    assert all(load_review(ws, pid)["switches"].values())


# ---- 写入可见性（2026-09-19）：节点产物增量播报 ----

def test_delta_note_reports_list_and_dict_changes(ws_factory):
    from novelist.forge.engine import _bp_counts, _delta_note

    ws, pid, _ = _seeded_project(ws_factory, "proj-delta")
    from novelist.forge.state import Blueprint
    bp = Blueprint.load(ws, pid)
    before = _bp_counts(bp)
    assert _delta_note(before, _bp_counts(bp)) == ""  # 无变化 → 空串
    bp.upsert("characters", {"id": "char:x", "name": "甲"})
    bp.set("worldview", {"name": "新世界"})
    note = _delta_note(before, _bp_counts(bp))
    assert "+1 人物" in note and "世界观已更新" in note


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
    # 已消耗 book 一次调用；再 build（仍有 pending）→ 零新调用直接交还。
    # 2026-09-19 口径修正：calls_used 是**本轮**计数（此前跨轮累计会让 resume
    # 起步即撞预算顶 = 续跑死锁）；闸门拦截本轮零消耗，与 324 行设计注释一致。
    assert res.calls_used == 1
    res2 = build(ws, pid, provider=ScriptedProvider([]), max_calls=60,
                 resume=True, deepen=False)
    assert res2.gate_halted and res2.calls_used == 0


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
        {"final": _node_reply(_event_stream_artifact(1))},
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
