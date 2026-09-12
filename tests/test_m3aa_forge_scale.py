"""M3aa 批次：ADR-033 Forge 硬边界规模适配的 A/B 单测。

A：列表型长尾（character/system/setting_entry）宽度放开（max_width_list），条目型保持 4；
B：max_calls 未显式给时按 N/K/M 规模推导（不再固定 60）。
全部确定性 Provider（docs/09 §2.1），不真调 LLM。
"""

from __future__ import annotations

from novelist.forge.engine import _build_call_budget, _child_width


# ---------- B · 预算推导 ----------

def test_budget_override_wins():
    assert _build_call_budget(3, 20, 8, override=60) == 60
    assert _build_call_budget(9, 60, 40, override=50) == 50


def test_budget_derived_non_trivial():
    # N=3,K=20,M=8 → 12+9+40+8=69
    assert _build_call_budget(3, 20, 8, override=None) == 69


def test_budget_large_scale_exceeds_old_default():
    # 长篇：N=5,K=30,M=30 → 12+15+60+min(30,24)=111 > 旧的固定 60
    b = _build_call_budget(5, 30, 30, override=None)
    assert b > 60 and b == 111


def test_budget_floor():
    assert _build_call_budget(0, 1, 0, override=None) >= 12


# ---------- A · 宽度分级 ----------

def test_width_wide_list_kinds():
    """character_group→character / worldview→system / system→setting_entry 放款到 max_width_list。"""
    assert _child_width("character_group", 4, 12) == 12
    assert _child_width("worldview", 4, 12) == 12
    assert _child_width("system", 4, 12) == 12


def test_width_item_kinds_keep_max():
    """volume→arc / chapter→beat 条目型维持 max_width=4。"""
    assert _child_width("volume", 4, 12) == 4
    assert _child_width("chapter", 4, 12) == 4


def test_width_unknown_kind_keeps_max():
    assert _child_width("bogus", 4, 12) == 4