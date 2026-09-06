"""事件级实然回写（ADR-013 完全体·2026-09-06 用户拍板）。

设计：不做平行"状态卡"，所有回写以事件为单位——worldstate（已有事件级）
扩展出角色卡实然同步；R-STATE 单调性基线改用独立 baselines 快照
（角色卡被实然同步后不能再当比较起点）；读取侧 _live_state_block 实时读盘。
"""

from __future__ import annotations

import json
from pathlib import Path

from novelist.storage.checkpoint import Checkpoint
from novelist.consistency.rules import run_state_checks
from novelist.core.orchestrator import _live_state_block
from novelist.core.worldstate import (apply_delta, init_from_bible, load,
                                      save)
from novelist.storage.workspace import Workspace


def _project(tmp_path, pid="proj-esync"):
    ws = Workspace(root=str(tmp_path))
    ws.create_project(pid)
    Checkpoint(ws).save(pid, {"id": pid, "title": "t", "pipeline_state": "正文",
                              "event_seq": 0})
    return ws, pid


def _write(ws, pid, rel, data):
    ws.write_json(ws._abs(f"{pid}/{rel}"), data)


def _seed(ws, pid):
    _write(ws, pid, "bible/characters.json", [
        {"id": "char:lf", "name": "苏晚", "gender": "male", "status": "active",
         "power": {"level": "炼气三层", "faction": "青云宗"},
         "first_appear": {"vol": 1, "ch": 1}, "is_protagonist": True},
        {"id": "char:twy", "name": "铁无涯", "gender": "male", "status": "active",
         "power": {"level": "金丹初期", "faction": "青云宗"},
         "first_appear": {"vol": 1, "ch": 1}},
    ])
    _write(ws, pid, "bible/worldview.json", {
        "name": "青冥界",
        "power_system": {"levels": ["炼气", "筑基", "金丹", "元婴"]},
        "rules": []})


def _card_level(ws, pid, cid) -> str | None:
    cards = json.loads(ws._abs(f"{pid}/bible/characters.json")
                       .read_text(encoding="utf-8"))
    for c in cards:
        if isinstance(c, dict) and c.get("id") == cid:
            return (c.get("power") or {}).get("level")
    return None


# ---------------------------------------------------------------- 写侧同步


def test_apply_delta_syncs_card_on_advance(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    init_from_bible(ws, pid)
    apply_delta(ws, pid, "char:lf", {"修为": "筑基初期"}, at={"vol": 1, "ch": 1})
    assert _card_level(ws, pid, "char:lf") == "筑基初期"


def test_apply_delta_keeps_card_on_regression(tmp_path):
    """倒退不同步卡：卡保留高水位（合法跌境须人工仲裁后再改）。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    init_from_bible(ws, pid)
    apply_delta(ws, pid, "char:lf", {"修为": "炼气二层"}, at={"vol": 1, "ch": 1})
    assert load(ws, pid)["characters"]["char:lf"]["realm"] == "炼气二层"  # 实然已变
    assert _card_level(ws, pid, "char:lf") == "炼气三层"  # 卡不动


def test_apply_delta_no_sync_out_of_system(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    init_from_bible(ws, pid)
    apply_delta(ws, pid, "char:lf", {"修为": "元神大圆满"}, at={"vol": 1, "ch": 1})
    assert _card_level(ws, pid, "char:lf") == "炼气三层"  # 体系外文本不入卡


def test_baselines_snapshot_immutable_by_resync(tmp_path):
    """baselines 记首次 init 的卡面值；后续 re-init 不覆盖、卡同步不影响。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    init_from_bible(ws, pid)
    apply_delta(ws, pid, "char:lf", {"修为": "筑基初期"}, at={"vol": 1, "ch": 1})
    init_from_bible(ws, pid)  # re-init 不得覆盖快照
    st = load(ws, pid)
    assert st["baselines"]["char:lf"] == "炼气三层"
    assert st["characters"]["char:lf"]["realm"] == "筑基初期"


# ---------------------------------------------------------------- R-STATE 锚


def test_rstate_uses_baselines_after_card_sync(tmp_path):
    """卡被实然同步后，R-STATE 仍能依据 baselines 锚检出倒退（核心契约）。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    init_from_bible(ws, pid)
    apply_delta(ws, pid, "char:lf", {"修为": "筑基初期"}, at={"vol": 1, "ch": 1})
    apply_delta(ws, pid, "char:lf", {"修为": "炼气二层"}, at={"vol": 1, "ch": 2})
    alerts = [a for a in run_state_checks(ws, pid) if a.rule_id == "R-STATE"]
    assert any(a.level == "block" and "倒退" in a.detail for a in alerts)


def test_rstate_level_skip_still_warns_after_card_sync(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    init_from_bible(ws, pid)
    apply_delta(ws, pid, "char:lf", {"修为": "元婴初期"}, at={"vol": 1, "ch": 1})
    alerts = [a for a in run_state_checks(ws, pid) if a.rule_id == "R-STATE"]
    assert any(a.level == "warn" and "越级" in a.detail for a in alerts)


# ---------------------------------------------------------------- 读取侧


def test_live_state_block_from_worldstate(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    init_from_bible(ws, pid)
    apply_delta(ws, pid, "char:lf", {"修为": "筑基初期", "位置": "藏经阁"},
                at={"vol": 1, "ch": 1})
    cards = json.loads(ws._abs(f"{pid}/bible/characters.json")
                       .read_text(encoding="utf-8"))
    block = _live_state_block(ws, pid, ["苏晚"], cards)
    assert "实然状态" in block
    assert "筑基初期" in block and "藏经阁" in block
    assert "以本块为准" in block  # 实然优先于计划态的仲裁指令


def test_live_state_block_empty_without_state(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)  # 未 init_from_bible、无 worldstate 记录
    cards = json.loads(ws._abs(f"{pid}/bible/characters.json")
                       .read_text(encoding="utf-8"))
    assert _live_state_block(ws, pid, ["苏晚"], cards) == ""


def test_live_state_block_ignores_unknown_cast(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    init_from_bible(ws, pid)
    cards = json.loads(ws._abs(f"{pid}/bible/characters.json")
                       .read_text(encoding="utf-8"))
    assert _live_state_block(ws, pid, ["路人甲"], cards) == ""  # 名册外角色跳过


def test_apply_delta_dead_flag_lands(tmp_path):
    """阵亡标志事件级落盘（下一事件的实然块会带"不得再出场行动"）。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    init_from_bible(ws, pid)
    apply_delta(ws, pid, "char:twy", {"阵亡": "是"}, at={"vol": 1, "ch": 2})
    assert load(ws, pid)["characters"]["char:twy"]["dead"] is True
