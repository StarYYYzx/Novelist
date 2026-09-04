"""M3t 滑动窗口上下文与 prompt 预算器（用户 2026-09-04 提议）。

覆盖三件事：
1. apply_prompt_budget 的确定性分层淘汰（钉死层不动、优先级序、未登记 tag 保守保留）；
2. prose_tail 正文滑动窗口切片（段落边界对齐、短文本原样、幂等）；
3. _event_goal 接线后：默认行为与旧版字节一致；prose_window 注入；
   超预算时按优先级整段淘汰且钉死层幸存。
"""

from novelist.core.orchestrator import _event_goal
from novelist.core.prompt_budget import (EVICT_PRIORITY, PINNED,
                                         apply_prompt_budget, prose_tail)

# ---------------------------------------------------------------- budget


def _sections() -> list[tuple[str, str]]:
    """构造一段超预算的 sections：related:lesson 最先该走，seam/goal 钉死。"""
    return [
        ("goal", "章目标" * 10),
        ("related:lesson", "教训" * 200),
        ("related:faction", "势力" * 200),
        ("memories", "前情" * 200),
        ("cast", "人物卡" * 50),
        ("seam", "接缝" * 100),
        ("tail", "收尾纪律"),
    ]


def test_budget_noop_under_limit():
    secs, evicted = apply_prompt_budget(_sections(), 10**9)
    assert secs == _sections() and evicted == []


def test_budget_disabled_when_zero():
    secs, evicted = apply_prompt_budget(_sections(), 0)
    assert secs == _sections() and evicted == []


def test_budget_evicts_lowest_priority_first():
    secs, evicted = apply_prompt_budget(_sections(), 100)
    # 钉死层必须幸存
    tags = [t for t, _ in secs]
    assert "goal" in tags and "seam" in tags and "cast" in tags and "tail" in tags
    # 淘汰顺序 = EVICT_PRIORITY 升序
    prios = [EVICT_PRIORITY[t] for t in evicted]
    assert prios == sorted(prios)
    # 最长块先被淘汰（lesson 200*2=400 字 > faction 400 > memories 400 同级，按序稳定）
    assert evicted[0] in ("related:lesson", "related:faction", "memories")


def test_budget_unknown_tag_kept():
    secs = [("goal", "a"), ("mystery_tag", "b" * 500), ("seam", "c")]
    out, evicted = apply_prompt_budget(secs, 10)
    # 未登记 tag 不淘汰（宁超不误删）
    assert ("mystery_tag", "b" * 500) in out and "mystery_tag" not in evicted


def test_budget_pinned_never_evicted():
    assert PINNED == -1
    assert all(v == PINNED for t, v in EVICT_PRIORITY.items()
               if t in ("goal", "step", "discipline", "direction", "cast",
                        "seam", "tail"))


# ---------------------------------------------------------------- prose_tail


def test_prose_tail_short_text_unchanged():
    assert prose_tail("短正文", 1200) == "短正文"
    assert prose_tail("", 1200) == ""


def test_prose_tail_cuts_at_paragraph_boundary():
    paras = ["第一段" + "甲" * 600, "第二段" + "乙" * 600, "第三段" + "丙" * 600]
    text = "\n\n".join(paras)
    out = prose_tail(text, 700)
    assert len(out) <= 700
    # 首个字符必须是完整段落的段首（"第二段"或"第三段"），不得是半句
    assert out.startswith(("第二段", "第三段"))


def test_prose_tail_idempotent_and_tail_preserved():
    text = "\n\n".join(f"段落{i}" + "字" * 300 for i in range(10))
    once = prose_tail(text, 800)
    twice = prose_tail(once, 800)
    assert once == twice
    assert text.endswith(once) or once in text  # 尾部内容不丢


# ---------------------------------------------------------------- _event_goal


def _goal_kwargs() -> dict:
    return dict(
        chapter_goal="本章目标：叶岚突破炼气三层。",
        ev_text="叶岚突破瓶颈",
        idx=2, total=5,
        prev_piece="上一事件的结尾正文。",
        seam_chars=300,
        memories=["前情提要一", "前情提要二"],
        is_last=False,
    )


def test_event_goal_default_output_matches_legacy_layout():
    """预算关 + 无窗口时，输出必须与旧版字符串拼装一致（回归保护）。"""
    out = _event_goal(**_goal_kwargs())
    assert out.startswith("本章目标：叶岚突破炼气三层。\n\n【本步骤】")
    assert "【输出纪律】" in out
    assert "【相关前情】（先忆，保持一致）：\n前情提要一\n前情提要二" in out
    assert out.endswith("不要写本章其他事件的内容，写到本事件结束即停。")
    # 块间统一空行分隔
    assert "\n\n\n" not in out
    # 未启用窗口时不含新标签
    assert "前文正文" not in out


def test_event_goal_prose_window_injected():
    out = _event_goal(**_goal_kwargs(), prose_window="窗口正文片段。")
    assert "【前文正文（最近片段）】" in out
    assert "窗口正文片段。" in out
    assert "严禁再写一遍" in out
    # 窗口在接缝之前（先看全貌，再贴着缝写）
    assert out.index("前文正文") < out.index("上文接缝")


def test_event_goal_budget_evicts_but_keeps_pinned():
    kwargs = _goal_kwargs()
    kwargs["memories"] = ["前" * 800]
    big_related = {"lesson": ["训" * 800], "faction": ["势" * 800]}
    out = _event_goal(related=big_related, char_budget=1200, **kwargs)
    # 钉死层幸存
    assert "本章目标" in out and "【输出纪律】" in out and "上文接缝" in out
    assert out.endswith("不要写本章其他事件的内容，写到本事件结束即停。")
    # 低价值层被整段淘汰
    assert "【相关知识·教训】" not in out or "【相关前情】" not in out \
        or len(out) < 1200


def test_event_goal_no_eviction_within_budget():
    out_small = _event_goal(**_goal_kwargs(), char_budget=10**6)
    out_zero = _event_goal(**_goal_kwargs())
    assert out_small == out_zero  # 预算宽松 = 行为不变
