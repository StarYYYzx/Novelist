"""事件流与切章（chapter_layout）+ 穿珠子位置视图（bead_view）测试。

对应 2026-09-19 两项拍板：
- **事件先行、章节后置**：规划只产卷级连续事件流，章边界由"目标字数 + 切点线索"
  （场景/视角/时间跳/高潮）确定性切出，标题沿用成稿后拟题；
- **穿珠子线索模型**：按计划区间判定本章在线的线 + 必须开启/收束 + 超期欠账，
  并给出【可用线 id 清单】（禁自造 id——真机实证的病根）。

全部为确定性逻辑（零 LLM），测试不走网络。
"""

from __future__ import annotations

import json

from novelist.core import chapter_layout as CL
from novelist.core import lines as L


def _ev(desc, **kw):
    row = {"desc": desc}
    row.update(kw)
    return row


# ---------------------------------------------------------------- 切章器

def test_slice_cuts_on_scene_and_pov_and_time():
    evs = [
        _ev("A1", scene="茶馆", pov="林源", est_words=1000, days=0),
        _ev("A2", scene="茶馆", pov="林源", est_words=1000, days=0),
        # 下一事件时间跳 → 在 A2 后切
        _ev("B1", scene="茶馆", pov="林源", est_words=800, days=3),
        _ev("B2", scene="茶馆", pov="苏清颜", est_words=800, days=0),
        _ev("B3", scene="茶馆", pov="苏清颜", est_words=800, days=0),
        # 下一事件换场景 → 在 B3 后切
        _ev("C1", scene="山道", pov="苏清颜", est_words=800, days=0),
    ]
    slices = CL.slice_chapters(evs, target_words=2400, min_words=1500, max_words=4200)
    # 切点优先级：climax → 时间跳 → 视角变 → 场景变 → 目标字数
    assert [s["break"] for s in slices[:3]] == ["time-skip", "scene-change", "stream-end"]
    assert slices[0]["ch"] == 1 and slices[0]["events"][0]["desc"] == "A1"
    # 事件不丢：所有切片的事件数之和 == 总事件数
    assert sum(len(s["events"]) for s in slices) == len(evs)


def test_slice_forces_cut_at_max_words_even_without_cue():
    evs = [_ev(f"e{i}", scene="同处", pov="林源", est_words=1500) for i in range(6)]
    # 目标字数抬到 5000：软目标不会先触发 → 逼出 max_words 硬切
    slices = CL.slice_chapters(evs, target_words=5000, min_words=4900, max_words=4200)
    assert slices[0]["break"] == "max-words"
    assert slices[0]["est_words"] >= 4200
    assert all(s["est_words"] <= 6000 for s in slices)


def test_slice_cuts_after_climax():
    evs = [_ev("铺垫", scene="s", pov="a", est_words=1000),
           _ev("爆点", scene="s", pov="a", est_words=900, climax=True),
           _ev("余波", scene="s", pov="a", est_words=700)]
    slices = CL.slice_chapters(evs, target_words=2400, min_words=1500, max_words=4200)
    assert slices[0]["break"] == "climax"
    assert [e["desc"] for e in slices[0]["events"]] == ["铺垫", "爆点"]


def test_slice_single_chapter_when_short_stream():
    slices = CL.slice_chapters([_ev("只有一条", scene="s", pov="a", est_words=300)],
                               target_words=2400, min_words=1500, max_words=4200)
    assert len(slices) == 1 and slices[0]["break"] == "stream-end"
    assert slices[0]["est_words"] == 300


def test_est_words_falls_back_to_desc_length():
    assert CL.est_words({"desc": "x" * 100}) >= 200          # 兜底估算有下限
    assert CL.est_words({"desc": "x" * 100, "est_words": 777}) == 777


def test_chapter_slice_and_count_bounds():
    evs = [_ev(f"e{i}", scene="s", pov="a", est_words=3000, climax=True) for i in range(3)]
    assert CL.chapter_count(evs) == 3
    assert CL.chapter_slice(evs, 2)["ch"] == 2
    assert CL.chapter_slice(evs, 4) is None      # 越界 → None（正文侧据此报"事件流已耗尽"）


# ---------------------------------------------------------------- 事件流 IO / 物化

def test_events_io_and_materialize_outline(ws_factory):
    ws, pid = ws_factory("proj-layout-io")
    evs = [
        _ev("林源在茶馆落脚", scene="茶馆", pov="林源", est_words=2000, days=0,
            characters=["char:linyuan"], threads_involved=["pt:x"],
            beads={"lines": [{"id": "ln:sub1", "action": "open", "note": "感情线开场"}]}),
        _ev("苏清颜上门", scene="茶馆", pov="苏清颜", est_words=2000, days=1,
            characters=["char:linyuan", "char:suqingyan"], climax=True),
    ]
    CL.save_events(ws, pid, 1, evs)
    assert CL.has_stream(ws, pid, 1)
    assert [e["desc"] for e in CL.load_events(ws, pid, 1)] == ["林源在茶馆落脚", "苏清颜上门"]

    sl = CL.chapter_slice(CL.load_events(ws, pid, 1), 1)
    p = CL.materialize_outline(ws, pid, 1, sl)
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    fm = json.loads(text.split("---")[1])
    assert fm["key_events"] == ["林源在茶馆落脚"]           # 切片内事件
    assert fm["after_days"] == 0
    assert fm["characters"] == ["char:linyuan"]
    assert fm["threads_involved"] == ["pt:x"]
    assert fm["lines_present"][0]["id"] == "ln:sub1"        # beads → lines_present
    assert "出场人物" in text


def test_summary_and_break_distribution(ws_factory):
    evs = [_ev(f"e{i}", scene="s", pov="a", est_words=1000, days=1 if i % 2 else 0)
           for i in range(6)]
    txt = CL.summary(evs, target_words=2000, min_words=1000, max_words=3000)
    assert "事件 6 条" in txt and "切出" in txt
    dist = CL.event_counts_by_break(evs, target_words=2000, min_words=1000, max_words=3000)
    assert sum(dist.values()) >= 2


def test_events_from_outlines_migrates_existing_gists(ws_factory):
    """存量项目：既有章细纲 → 事件流迁移（不重跑 build 也能用新流程）。"""
    ws, pid = ws_factory("proj-layout-mig")
    from novelist.forge.state import Blueprint

    bp = Blueprint.blank()
    bp.data["meta"] = {"title": "t", "genre": "修仙", "logline": "x",
                       "scale": {"volumes": 1, "chapters_per_volume": 2,
                                 "target_words_per_chapter": 100}}
    bp.save(ws, pid)
    from novelist.forge.nodes import render_gist_md

    for ch, evs in ((1, ["事件A", "事件B"]), (2, ["事件C"])):
        p = ws.outline_chapter_path(pid, 1, ch)
        p.parent.mkdir(parents=True, exist_ok=True)
        ws.write_text(p, render_gist_md(
            {"title": f"章{ch}", "key_events": evs, "pov": "林源", "after_days": 2, "turns": []},
            1, ch, []))
    migrated = CL.events_from_outlines(ws, pid, 1)
    assert [e["desc"] for e in migrated] == ["事件A", "事件B", "事件C"]
    assert migrated[0]["days"] == 2          # 章首事件承接章间时间跨度
    assert migrated[1]["days"] == 0
    assert migrated[0]["source_ch"] == 1 and migrated[2]["source_ch"] == 2


# ---------------------------------------------------------------- 穿珠子 bead_view

def _ledger():
    return [
        L.new_line("ln:main", "仙帝隐居主线", kind="main", carrier="theme",
                   target={"vol": 4, "note": "终老"}, status="active"),
        L.new_line("ln:sub1", "感情线", kind="subplot", carrier="emotion",
                   planned_span={"start_ch": 3, "end_ch": 12}, status="dormant"),
        L.new_line("ln:sub2", "追查线", kind="subplot", carrier="character",
                   planned_span={"start_ch": 5, "end_ch": 20}, status="active"),
        L.new_line("ln:hi1", "地脉传送阵", kind="hidden", carrier="object",
                   reveal_points=[{"vol": 1, "ch": 11}], status="dormant"),
    ]


def test_bead_view_marks_opening_and_closing_due():
    led = _ledger()
    blk, warns = L.bead_view(led, [], 1, 3, 20)
    assert "【本章必须开启】" in blk and "ln:sub1" in blk
    # 区间终点：已开线 → 必须收束（含 yield 提示）
    led2 = _ledger()
    led2[1]["status"] = "active"
    blk2, _ = L.bead_view(led2, [], 1, 12, 20)
    assert "【本章必须收束】" in blk2 and "yield" in blk2


def test_bead_view_never_opened_counts_as_overdue():
    """区间走完仍 dormant 的支线 = 漏开欠账，必须点名（而不是要求它"收束"）。"""
    led = _ledger()
    led[1]["status"] = "dormant"          # sub1 区间 3-12，从未开启
    blk, warns = L.bead_view(led, [], 1, 14, 20)   # 宽限 1 章后计入欠账
    assert "【超期欠账】" in blk and "ln:sub1" in blk
    assert "【本章必须收束】" not in blk
    assert any("超期欠账" in w for w in warns)


def test_bead_view_whitelists_usable_ids_and_bans_dangling():
    """真机病根：旧版只注入 active → 模型自造 ln:jiuzhu；现必须给出白名单与禁令。"""
    blk, _ = L.bead_view(_ledger(), [], 1, 1, 20)
    assert "【可用线 id 清单】" in blk
    for lid in ("ln:main", "ln:sub1", "ln:sub2", "ln:hi1"):
        assert lid in blk
    assert "禁止自造 ln: id" in blk


def test_bead_view_hidden_reveal_point_and_threads_window():
    led = _ledger()
    threads = [{"id": "pt:jiuzhu", "desc": "劫息外泄", "status": "planted",
                "parent_line": "ln:main",
                "payoff_window": {"vol": 1, "start_ch": 10, "end_ch": 14}}]
    blk, _ = L.bead_view(led, threads, 1, 11, 20)
    assert "ln:hi1" in blk and "【伏笔回收窗口命中本章】" in blk and "pt:jiuzhu" in blk
    # 超窗 → 欠账
    blk2, _ = L.bead_view(led, threads, 1, 20, 20)
    assert "【伏笔超窗未回收】" in blk2


def test_bead_view_empty_ledger_no_injection():
    assert L.bead_view([], [], 1, 1, 20) == ("", [])


def test_span_helpers_validate():
    assert L._norm_span({"start_ch": 5, "end_ch": 3}) is None      # 起 > 止 → 拒收
    assert L._norm_span({"start_ch": 0, "end_ch": 9}) is None      # 1 基
    assert L.span_of({"planned_span": {"vol": 2, "start_ch": 1, "end_ch": 4}}) == (2, 1, 4)
    assert L.span_of({}) is None


def test_dangling_line_declaration_becomes_pending_proposal(ws_factory):
    """lines_present 声明账本外的 id → 登记为待转正提案（旧行为是静默丢弃）。"""
    ws, pid = ws_factory("proj-dangle-line")
    led = _ledger()
    warns = L.apply_chapter_actions(ws, pid, led, 1, 3, [
        {"id": "ln:jiuzhu", "action": "advance", "note": "自造 id（照 pt: 起名）"},
    ])
    assert any("待转正提案" in w for w in warns), warns
    pend = [x for x in led if x["status"] == "pending"]
    assert [x["id"] for x in pend] == ["ln:jiuzhu"]
