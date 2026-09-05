"""方案6.1/6.2（质量加固 2026-09-05）：首登场硬约束 + 广播拒绝点名禁令。

- first_seen / banned 两个钉死层（预算器永不淘汰）；
- 首次出场身份线索检查进审校清单（修复调用兜底）。
"""

from __future__ import annotations

from novelist.core.orchestrator import (_event_goal, _first_appearance_problems,
                                        _has_identity_cue)
from novelist.core.entity import EntityTracker
from novelist.core.prompt_budget import EVICT_PRIORITY, PINNED, apply_prompt_budget


# ---- 身份线索启发式 ----

def test_identity_cue_positive():
    assert _has_identity_cue("林婉清站在门口。她是李天劫的学姐，笑着打招呼。", "林婉清")
    assert _has_identity_cue("迎面走来一位少女，名叫林婉清。", "林婉清")


def test_identity_cue_negative():
    text = "林婉清站在门口，看了李天劫两眼，随后转身去了食堂。"
    assert not _has_identity_cue(text, "林婉清")


def test_identity_cue_missing_name_is_neutral():
    # 正文未字面点名（别名出场）不判——误杀率优先压低
    assert _has_identity_cue("两人一起去了食堂。", "林婉清")


# ---- 首次出场审校问题 ----

def _tracker():
    t = EntityTracker()
    t._register("char:lin", "character", ["林婉清"])
    return t


def test_first_appearance_problem_raised():
    t = _tracker()
    text = "林婉清站在门口，看了李天劫两眼，随后转身去了食堂。"
    probs = _first_appearance_problems(t, text)
    assert len(probs) == 1 and "林婉清" in probs[0]


def test_first_appearance_ok_with_cue():
    t = _tracker()
    text = "林婉清站在门口。她是李天劫的学姐，笑着打招呼。"
    assert _first_appearance_problems(t, text) == []


def test_first_appearance_ignores_known_entities():
    t = _tracker()
    e = t.entities["char:lin"]
    e.stage, e.first_ch = "described", 2  # 前章已介绍
    text = "林婉清站在门口，看了李天劫两眼。"
    assert _first_appearance_problems(t, text) == []


def test_first_appearance_none_tracker():
    assert _first_appearance_problems(None, "任意文本") == []


# ---- prompt 钉死层 ----

def test_event_goal_contains_first_seen_and_banned():
    prompt = _event_goal("本章目标", "事件一", 1, 2, "", 300, [], is_last=False,
                         first_seen_lines=["- 「林婉清」首次出场：须交代身份"],
                         banned_names=["苏晚晴"])
    assert "【首次出场人物·硬性要求】" in prompt
    assert "【点名禁令】" in prompt and "苏晚晴" in prompt


def test_first_seen_and_banned_are_pinned():
    assert EVICT_PRIORITY.get("first_seen") == PINNED
    assert EVICT_PRIORITY.get("banned") == PINNED
    blocks = [("first_seen", "首" * 600), ("memories", "记" * 600),
              ("banned", "禁" * 600)]
    kept, evicted = apply_prompt_budget(blocks, 800)
    tags = [t for t, _ in kept]
    assert "first_seen" in tags and "banned" in tags
    assert "memories" in evicted
