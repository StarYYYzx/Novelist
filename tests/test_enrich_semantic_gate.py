"""A3 语义闸门（enrich 提案 vs 实然账本/未写章细纲前提）测试（2026-09-04）。

覆盖：
- 账本冲突（账本"记底细/暗中"态 vs 提案"坦诚亲近"断言）→ 拒绝原因；
- 账本亲近态 vs 提案敌对断言 → 拒绝原因；
- 账本一致（暗中留意 vs 提案暗中留意）→ 放行；
- 无账本记录 → 闸门不作为；
- 未写章细纲"首次登场"前提 vs 提案"旧识"断言 → notes 备注（不拒，人工拍板）；
- 已写章不触发前提比对；
- notes 展示在 list_pending / 落进 pending 条目。

词级信号零模型调用；纪律：单元测试绝不真调 LLM（docs/09 §2.1）。
"""

from __future__ import annotations

import json

from novelist.core.character_enrich import (list_pending, load_pending, propose,
                                            semantic_gate)
from novelist.storage.checkpoint import Checkpoint
from novelist.storage.workspace import Workspace


def _project(tmp_path, pid="proj-sem"):
    ws = Workspace(root=str(tmp_path))
    ws.create_project(pid)
    Checkpoint(ws).save(pid, {"id": pid, "title": "测试书", "pipeline_state": "正文",
                              "event_seq": 0})
    return ws, pid


def _seed(ws, pid):
    ws.write_json(ws._abs(f"{pid}/bible/characters.json"), [
        {"id": "char:yelan", "name": "叶岚", "gender": "male",
         "power": {"level": "炼气三层", "faction": "青云宗"}},
        {"id": "char:wan", "name": "林婉儿", "gender": "female",
         "power": {"level": "练气期", "faction": "无"}},
        {"id": "char:su", "name": "苏老", "gender": "male",
         "power": {"level": "金丹期", "faction": "协会"}},
    ])
    ws.write_json(ws._abs(f"{pid}/bible/worldview.json"), {
        "name": "青冥界", "power_system": {"levels": ["炼气", "金丹"]},
        "factions": [{"faction": "青云宗"}]})


def _card():
    return {"id": "char:su", "name": "苏老"}


def test_ledger_hidden_state_rejects_close_claim(tmp_path):
    """账本「记下其特殊底细」（隐瞒态）vs 提案「坦诚相待毫无保留」→ 拒。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    ws.write_json(ws._abs(f"{pid}/memory/relationship_ledger.json"), {
        "schema": "rel-ledger-v1",
        "pairs": [{"a": "char:wan", "b": "char:su", "state": "记下其特殊底细",
                   "trend": "转向"}]})
    proposal = {"name": "苏老", "behavior_rules": ["a", "b"],
                "relationships": [{"target": "林婉儿", "type": "坦诚相待，毫无保留"}]}
    reasons, notes = semantic_gate(_card(), proposal, ws=ws, project_id=pid)
    assert any("实然账本冲突" in r for r in reasons) and notes == []


def test_ledger_close_state_rejects_hostile_claim(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    ws.write_json(ws._abs(f"{pid}/memory/relationship_ledger.json"), {
        "schema": "rel-ledger-v1",
        "pairs": [{"a": "char:wan", "b": "char:su", "state": "救命之恩，视为亲传",
                   "trend": "升温"}]})
    proposal = {"name": "苏老", "behavior_rules": ["a", "b"],
                "relationships": [{"target": "林婉儿", "type": "不共戴天的仇人"}]}
    reasons, _notes = semantic_gate(_card(), proposal, ws=ws, project_id=pid)
    assert any("实然账本冲突" in r for r in reasons)


def test_consistent_ledger_state_passes(tmp_path):
    """账本「记下其特殊底细」vs 提案「暗中留意」→ 一致，放行。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    ws.write_json(ws._abs(f"{pid}/memory/relationship_ledger.json"), {
        "schema": "rel-ledger-v1",
        "pairs": [{"a": "char:wan", "b": "char:su", "state": "记下其特殊底细",
                   "trend": "转向"}]})
    proposal = {"name": "苏老", "behavior_rules": ["a", "b"],
                "relationships": [{"target": "林婉儿", "type": "暗中留意其根脚"}]}
    reasons, notes = semantic_gate(_card(), proposal, ws=ws, project_id=pid)
    assert reasons == [] and notes == []


def test_no_ledger_record_inert(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    proposal = {"name": "苏老", "behavior_rules": ["a", "b"],
                "relationships": [{"target": "叶岚", "type": "坦诚相待"}]}
    reasons, notes = semantic_gate(_card(), proposal, ws=ws, project_id=pid)
    assert reasons == [] and notes == []


def test_unwritten_outline_first_time_vs_old_tie_notes(tmp_path):
    """未写章细纲「首次」+ 提案「旧识」→ notes 备注（不拒，人工拍板）。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    ol = ws._abs(f"{pid}/outline/chapters")
    ol.mkdir(parents=True, exist_ok=True)
    (ol / "1-6.md").write_text(
        "---\n"
        'key_events: ["协会正式吸收李天劫，陈老师任联络人", "林婉儿首次显露修为"]\n'
        "---\n正文", encoding="utf-8")
    proposal = {"name": "苏老", "behavior_rules": ["a", "b"],
                "relationships": [{"target": "林婉儿", "type": "多年旧识，早已相识"}]}
    reasons, notes = semantic_gate(_card(), proposal, ws=ws, project_id=pid)
    assert reasons == []
    assert any("1-6" in n and "旧识" in n for n in notes)


def test_written_chapter_no_premise_check(tmp_path):
    """已写章（drafts 存在）不再做前提比对。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    ol = ws._abs(f"{pid}/outline/chapters")
    ol.mkdir(parents=True, exist_ok=True)
    (ol / "1-6.md").write_text(
        "---\n"
        'key_events: ["林婉儿首次显露修为"]\n'
        "---\n正文", encoding="utf-8")
    d = ws._abs(f"{pid}/drafts/chapters")
    d.mkdir(parents=True, exist_ok=True)
    (d / "1-6.md").write_text("正文已写。", encoding="utf-8")
    proposal = {"name": "苏老", "behavior_rules": ["a", "b"],
                "relationships": [{"target": "林婉儿", "type": "多年旧识"}]}
    reasons, notes = semantic_gate(_card(), proposal, ws=ws, project_id=pid)
    assert reasons == [] and notes == []


def test_notes_flow_into_pending_and_listing(tmp_path, monkeypatch):
    """notes 落进 pending 条目并在 list_pending 展示（propose 全链路，StubLLM 提案）。"""
    from tests.conftest import StubLLM

    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    ol = ws._abs(f"{pid}/outline/chapters")
    ol.mkdir(parents=True, exist_ok=True)
    (ol / "1-6.md").write_text(
        "---\n"
        'key_events: ["林婉儿首次显露修为"]\n'
        "---\n正文", encoding="utf-8")
    reply = json.dumps({"name": "苏老", "age": None,
                        "relationships": [{"target": "林婉儿", "type": "多年旧识"}],
                        "behavior_rules": ["不轻易出手", "护短"]}, ensure_ascii=False)
    res = propose(ws, pid, StubLLM(reply), card_ids=["char:su"])
    assert res[0]["ok"] and res[0]["proposed"]
    entry = load_pending(ws, pid)[0]
    assert any("旧识" in n for n in (entry.get("notes") or []))
    assert any("旧识" in line for line in list_pending(ws, pid))
