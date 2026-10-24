"""章级事实账本（DL-009-②·2026-10-09）：确定性事实注入的存取与裁剪。

对应 docs/崩溃模式分类表-2026-10-09.md C4 类崩法（事件间各自编规则、
谎言不自洽）的预防层；设计哲学：防线进流程，不走 RAG 概率命中。
"""

from __future__ import annotations

from novelist.core.chapter_facts import (
    CARRYOVER,
    MAX_ENTRIES,
    carryover_lines,
    load_entries,
    record,
    save,
)
from novelist.core.orchestrator import _event_goal
from novelist.core.prompt_budget import apply_prompt_budget
from novelist.storage.checkpoint import Checkpoint
from novelist.storage.workspace import Workspace


def _project(tmp_path, pid="proj-cfacts"):
    ws = Workspace(root=str(tmp_path))
    ws.create_project(pid)
    Checkpoint(ws).save(pid, {"id": pid, "title": "t", "pipeline_state": "正文",
                              "event_seq": 0})
    return ws, pid


# ---------------------------------------------------------------- 存取与裁剪


def test_load_missing_returns_empty(tmp_path):
    ws, pid = _project(tmp_path)
    assert load_entries(ws, pid) == []


def test_record_replace_is_idempotent(tmp_path):
    """同章重跑替换不累加（幂等），超额丢最旧。"""
    facts = [f"事实{i}" for i in range(15)]
    e1 = record([], 1, 3, facts)
    e2 = record(e1, 1, 3, facts)  # 同章重跑
    assert e2 == e1
    cur = e2[0]
    assert cur["facts"] == [f"事实{i}" for i in range(3, 15)]  # 只留最近 12 条
    assert len(e2) == 1  # 替换不累加


def test_record_prunes_to_recent_two_chapters(tmp_path):
    entries: list[dict] = []
    entries = record(entries, 1, 1, ["a"])
    entries = record(entries, 1, 2, ["b"])
    entries = record(entries, 1, 3, ["c"])
    assert [(e["vol"], e["ch"]) for e in entries] == [(1, 2), (1, 3)]
    assert len(entries) == MAX_ENTRIES


def test_carryover_takes_strictly_previous_chapter(tmp_path):
    entries = record([], 1, 2, ["前1", "前2", "前3"])
    assert carryover_lines(entries, 1, 3) == ["前1", "前2", "前3"]
    assert carryover_lines(entries, 1, 2) == []  # 本章自己不算承接
    assert carryover_lines(entries, 1, 1) == []  # 未来章更不算


def test_carryover_caps_at_limit(tmp_path):
    facts = [f"事实{i}" for i in range(10)]
    entries = record([], 1, 2, facts)
    lines = carryover_lines(entries, 1, 3)
    assert len(lines) == CARRYOVER
    assert lines == facts[-CARRYOVER:]


def test_save_and_load_roundtrip(tmp_path):
    ws, pid = _project(tmp_path)
    entries = record([], 1, 1, ["系统已绑定"])
    save(ws, pid, entries)
    assert load_entries(ws, pid) == entries


# ---------------------------------------------------------------- 注入与预算


def test_event_goal_renders_facts_block():
    goal = _event_goal("写第一章", "主角觉醒系统", 2, 3, "", 300, [], False,
                       fact_lines=["- 系统绑定完成，规则不可拒绝"])
    assert "【本章已确立事实】" in goal
    assert "系统绑定完成" in goal
    assert "不得与之矛盾" in goal


def test_event_goal_no_facts_block_when_empty():
    goal = _event_goal("写第一章", "主角觉醒系统", 1, 3, "", 300, [], False)
    assert "【本章已确立事实】" not in goal


def test_facts_block_is_pinned_under_budget():
    """事实账本是钉死层：预算超限时不得被淘汰（确定性事实宁超不丢）。"""
    sections = [("goal", "目标" * 50), ("facts", "【本章已确立事实】事实" * 50),
                ("memories", "前情" * 200)]
    # DL-015 约束闭包：预算须容得下钉死层（goal+facts=650），否则编译失败而非超着跑
    kept, evicted = apply_prompt_budget(sections, 700)
    tags = [t for t, _ in kept]
    assert "facts" in tags and "goal" in tags
    assert "memories" in evicted
