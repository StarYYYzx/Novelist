"""M10 里程碑测试（docs/08 M3m）：时间轴与定时事件（ADR-019）。

覆盖：
- T1 数据层：天数解析、worldstate time/pending、timeline.json 写入、
  编纂员「时间：/约定：」行抽取与提交、unavailable_until 登记；
- T2 编排器：分档提醒注入、软 block 拦截、章末记账（fired/expired）；
- T3 一致性规则：R-TL 按 t 单调、R-TIME 到期告警、R-STATE 不可出场期。

单元测试绝不真调 LLM（docs/09 §2.1）：LLM 走 `stub_llm` 夹具。
"""

from __future__ import annotations

import json

from novelist.consistency import run_consistency
from novelist.consistency.rules import run_state_checks
from novelist.core import timeline as tl
from novelist.core import worldstate
from novelist.core.chronicler import Chronicler
from novelist.core.orchestrator import produce_chapter


def _seed_chars(ws, pid, write):
    write(ws, pid, "bible/characters.json", [
        {"id": "char:yelan", "name": "叶蓝", "gender": "male", "status": "active",
         "power": {"level": "炼气三层", "faction": "青云宗"},
         "first_appear": {"vol": 1, "ch": 1}, "is_protagonist": True},
        {"id": "char:sw", "name": "苏晚", "gender": "female", "status": "active",
         "power": {"level": "炼气四层", "faction": "青云宗"},
         "first_appear": {"vol": 1, "ch": 1}},
    ])


# ---------------------------------------------------------------- T1 数据层


def test_parse_duration_numeric():
    assert tl.parse_duration("+90日") == (90, None)
    assert tl.parse_duration("90天") == (90, None)
    assert tl.parse_duration("2周") == (14, None)
    assert tl.parse_duration("3年") == (1095, None)
    assert tl.parse_duration("3651日") == (3651, None)  # 超上限不在此处判，调用方 warn


def test_parse_duration_flashback_same_day():
    assert tl.parse_duration("闪回")[0] is None
    assert tl.parse_duration("回忆")[0] is None
    assert tl.parse_duration("同日") == (0, None)
    assert tl.parse_duration("与此同时") == (0, None)


def test_parse_duration_quantifier_fallback():
    assert tl.parse_duration("三月") == (90, None)
    assert tl.parse_duration("三个月") == (90, None)
    assert tl.parse_duration("半月") == (15, None)
    assert tl.parse_duration("三年") == (1095, None)


def test_parse_duration_garbage_warns():
    dt, warn = tl.parse_duration("若干时辰")
    assert dt is None and warn


def test_parse_time_line_with_note():
    assert tl.parse_time_line("时间：+90日") == (90, "", None)
    assert tl.parse_time_line("时间：+90日 | 叶蓝闭关结束") == (90, "叶蓝闭关结束", None)
    dt, _, _ = tl.parse_time_line("时间：闪回")
    assert dt is None


def test_parse_pending_line():
    parsed = tl.parse_pending_line("约定：叶蓝出关｜+90日")
    assert parsed == ("叶蓝出关", 90, None)
    assert tl.parse_pending_line("约定：叶蓝出关") is None  # 缺竖线不登记
    assert tl.parse_pending_line("事件：xxx | conflict") is None  # 非约定行


def test_advance_moves_now_and_appends_timeline(ws_factory, write_json):
    ws, pid = ws_factory()
    assert tl.advance(ws, pid, 90, vol=1, ch=2) == 90
    entries = tl.load_timeline(ws, pid)
    assert len(entries) == 1 and entries[0]["at"]["t"] == 90
    assert entries[0]["in_chapters"] == [{"vol": 1, "ch": 2}]
    # id 递增
    tl.advance(ws, pid, 0, vol=1, ch=3)  # 同日：不推进 but 登记
    assert tl.load_timeline(ws, pid)[-1]["id"] == "tl:2"
    st = worldstate.load(ws, pid)
    assert tl.now_of(st) == 90


def test_add_pending_sets_due_and_span(ws_factory):
    ws, pid = ws_factory()
    item = tl.add_pending(ws, pid, what="叶蓝出关", dt=90, vol=1, ch=2)
    assert item["due"] == 90 and item["span"] == 90
    assert item["status"] == "scheduled" and item["who"] == ""
    st = worldstate.load(ws, pid)
    assert len(tl.pending_of(st)) == 1
    # id 递增
    tl.add_pending(ws, pid, what="宗门大比", dt=30, vol=1, ch=3)
    assert tl.pending_of(worldstate.load(ws, pid))[-1]["id"] == "pd:2"


def test_add_pending_unavailable_until(ws_factory):
    """约定文本命中不可出场词（闭关）→ 人物 unavailable_until = due。"""
    ws, pid = ws_factory()
    item = tl.add_pending(ws, pid, what="叶蓝闭关三月", dt=90, vol=1, ch=2,
                          who="char:yelan",
                          unavailable_states=("闭关", "失踪"))
    assert item["who"] == "char:yelan"
    st = worldstate.load(ws, pid)
    cur = st["characters"]["char:yelan"]
    assert cur["unavailable_until"] == 90
    assert cur["unavailable_reason"] == "闭关"


def test_worldstate_init_from_bible_sets_time_zero(ws_factory, write_json):
    ws, pid = ws_factory()
    _seed_chars(ws, pid, write_json)
    state = worldstate.init_from_bible(ws, pid)
    assert state["time"]["now"] == 0
    assert state["pending"] == []
    assert state["time"]["origin_text"]  # 非空


def test_chronicler_extracts_time_and_pending_lines(ws_factory, write_json, stub_llm):
    ws, pid = ws_factory()
    _seed_chars(ws, pid, write_json)
    reply = ("叶蓝闭关修炼 | discovery | 叶蓝\n"
             "状态：叶蓝 | 修为：炼气三层\n"
             "时间：+90日 | 叶蓝闭关结束\n"
             "约定：叶蓝出关｜+90日\n")
    c = Chronicler(ws, pid, llm=stub_llm(reply))
    ex = c.extract("正文略")
    assert len(ex.events) == 1
    assert ex.time_lines and ex.pending_lines
    # 状态/时间/约定行不得被当成事件行
    assert all(not e.summary.startswith(("状态", "时间", "约定")) for e in ex.events)


def test_chronicler_commit_applies_time_and_pending(ws_factory, write_json, stub_llm):
    ws, pid = ws_factory()
    _seed_chars(ws, pid, write_json)
    reply = ("叶蓝闭关修炼 | discovery | 叶蓝\n"
             "状态：叶蓝 | 修为：炼气三层\n"
             "时间：+90日 | 叶蓝闭关结束\n"
             "约定：叶蓝闭关三月｜+90日\n")
    report = Chronicler(ws, pid, llm=stub_llm(reply)).run("正文略", 1, 2)
    st = worldstate.load(ws, pid)
    assert tl.now_of(st) == 90
    assert report.time_advanced == 90
    assert report.pending_added  # [(id, what, due)]
    assert st["characters"]["char:yelan"]["unavailable_until"] == 90
    # 状态历史带 t（ADR-019 交叉核对用）
    assert st["characters"]["char:yelan"]["history"][-1]["at"]["t"] == 90
    # timeline 有两条：推进 + 0? 至少推进那条存在
    entries = tl.load_timeline(ws, pid)
    assert any(e["at"]["t"] == 90 for e in entries)


# ---------------------------------------------------------------- T2 分档与记账


def _pending_state(*, due=90, span=90, status="scheduled", overdue=0, block_count=0):
    return {"time": {"now": 0, "origin_text": "开书之日"},
            "pending": [{"id": "pd:1", "who": "char:yelan", "what": "叶蓝出关",
                         "due": due, "span": span, "status": status,
                         "overdue": overdue, "block_count": block_count}],
            "characters": {}}


def test_reminder_tiers():
    # > 30% 静默
    assert tl.reminder_lines(_pending_state(due=100)) == []
    # ≤ 30% 轻提示
    light = tl.reminder_lines(_pending_state(due=25))
    assert any("临近事项" in ln for ln in light) and not any("宜安排" in ln for ln in light)
    # ≤ 10% 强提示
    strong = tl.reminder_lines(_pending_state(due=9))
    assert any("宜安排" in ln for ln in strong)
    # 已到期 强提示
    overdue = tl.reminder_lines(_pending_state(due=-5))
    assert any("宜安排" in ln for ln in overdue)


def test_match_pending_heuristic():
    assert tl.match_pending("叶蓝出关", "他于今日出关")
    assert tl.match_pending("叶蓝出关", "宗门日常琐事") is False  # 完全无关
    assert tl.match_pending("宗门大比开始", "大比正式开始，各峰弟子入场")


def test_soft_block_check(ws_factory, write_json):
    ws, pid = ws_factory()
    _seed_chars(ws, pid, write_json)
    tl.add_pending(ws, pid, what="叶蓝出关", dt=1, vol=1, ch=1, who="char:yelan")
    # 手工把 overdue 抬到 block 线
    st = worldstate.load(ws, pid)
    st["pending"][0]["overdue"] = 5
    worldstate.save(ws, pid, st)
    # 细纲没提 → 拦截
    blocks = tl.soft_block_check(ws, pid, "苏晚巡视山门")
    assert blocks and "软 block" in blocks[0]
    # 细纲提到 → 放行
    assert tl.soft_block_check(ws, pid, "本章宜安排：叶蓝出关，突破筑基") == []


def test_tick_fires_and_clears_unavailable(ws_factory, write_json):
    ws, pid = ws_factory()
    _seed_chars(ws, pid, write_json)
    tl.add_pending(ws, pid, what="叶蓝闭关出关", dt=90, vol=1, ch=2, who="char:yelan",
                   unavailable_states=("闭关",))
    st0 = worldstate.load(ws, pid)
    assert st0["characters"]["char:yelan"]["unavailable_until"] == 90
    rep = tl.tick(ws, pid, vol=1, ch=3, chapter_text="叶蓝终于出关")
    assert rep.fired == ["pd:1"]
    st = worldstate.load(ws, pid)
    assert st["pending"][0]["status"] == "fired"
    assert "unavailable_until" not in st["characters"]["char:yelan"]


def test_tick_fires_by_name_when_lexically_disjoint(ws_factory, write_json):
    """词面不重合兜底：约定「闭关三月」、正文「出关」——靠姓名+已到期判定兑现。"""
    ws, pid = ws_factory()
    _seed_chars(ws, pid, write_json)
    tl.add_pending(ws, pid, what="叶蓝闭关三月", dt=90, vol=1, ch=2, who="char:yelan",
                   unavailable_states=("闭关",))
    tl.advance(ws, pid, 90, vol=1, ch=3)  # 时间已过 due
    rep = tl.tick(ws, pid, vol=1, ch=4, chapter_text="叶蓝出关，气息节节攀升")
    assert rep.fired == ["pd:1"]
    assert worldstate.load(ws, pid)["pending"][0]["status"] == "fired"


def test_tick_escalation_to_expired(ws_factory):
    """到期后连写 7 章、章章不兑现 → warn → 软 block×3 → 自动 expired 放行。"""
    ws, pid = ws_factory()
    tl.add_pending(ws, pid, what="叶蓝出关", dt=1, vol=1, ch=1)
    tl.advance(ws, pid, 10, vol=1, ch=1)  # 时间越过 due=1
    saw_block = False
    for ch in range(2, 9):
        rep = tl.tick(ws, pid, vol=1, ch=ch, chapter_text="宗门日常琐事")
        if any("软 block" in w for w in rep.warnings):
            saw_block = True
    assert saw_block, "逾期 ≥5 章应出现软 block 告警"
    st = worldstate.load(ws, pid)
    p = st["pending"][0]
    assert p["status"] == "expired", f"应自动作废，实际 {p['status']}"
    assert p["block_count"] >= 3


def test_produce_chapter_injects_reminder_and_ticks(ws_factory, write_json, stub_llm):
    """编排器集成：分档提醒进 goal prompt；章末记账进 pending_tick。"""
    ws, pid = ws_factory()
    _seed_chars(ws, pid, write_json)
    tl.add_pending(ws, pid, what="叶蓝出关", dt=30, vol=1, ch=1)  # span 30 → ≤30% 轻提示
    prov = stub_llm("叶蓝在山中赶路，忽见天光破晓。" * 20)
    res = produce_chapter(ws, pid, 1, 1, prov, prefer_direct=True,
                          jit_characters=False, validate=False, polish=False)
    assert res.ok, res.result
    # 提醒注入：goal 包含「临近事项」
    assert any("叶蓝出关" in c for c in prov.calls), "临近事项应注入生成 prompt"
    # 章末记账执行过（fired 与否取决于正文；至少结构存在）
    assert isinstance(res.pending_tick, dict)


def test_produce_chapter_soft_block(ws_factory, write_json, stub_llm):
    """软 block：被拦截的 pending 未进细纲 → ok=False；连拦 3 次自动放行。"""
    ws, pid = ws_factory()
    _seed_chars(ws, pid, write_json)
    tl.add_pending(ws, pid, what="叶蓝出关", dt=1, vol=1, ch=1)
    st = worldstate.load(ws, pid)
    st["pending"][0]["overdue"] = 5
    worldstate.save(ws, pid, st)
    prov = stub_llm("正文。" * 50)
    res = produce_chapter(ws, pid, 1, 2, prov, prefer_direct=True,
                          jit_characters=False, validate=False)
    assert res.ok is False
    assert "软 block" in res.result


# ---------------------------------------------------------------- T3 一致性规则


def test_rtl_monotonic_by_t(ws_factory, write_json):
    ws, pid = ws_factory()
    write_json(ws, pid, "bible/timeline.json", [
        {"id": "tl:1", "event": "服丹闭关", "at": {"t": 10, "vol": 1, "ch": 1},
         "in_chapters": [{"vol": 1, "ch": 1}]},
        {"id": "tl:2", "event": "出关", "at": {"t": 8, "vol": 1, "ch": 2},   # t 倒退
         "in_chapters": [{"vol": 1, "ch": 2}]},
    ])
    alerts = run_consistency(ws, pid)
    tl_alerts = [a for a in alerts if a.rule_id == "R-TL"]
    assert any("早于" in a.detail for a in tl_alerts)


def test_rtl_monotonic_ok_by_t(ws_factory, write_json):
    ws, pid = ws_factory()
    write_json(ws, pid, "bible/timeline.json", [
        {"id": "tl:1", "event": "服丹闭关", "at": {"t": 10, "vol": 1, "ch": 1},
         "in_chapters": [{"vol": 1, "ch": 1}]},
        {"id": "tl:2", "event": "出关", "at": {"t": 100, "vol": 1, "ch": 15},
         "in_chapters": [{"vol": 1, "ch": 15}]},
    ])
    alerts = run_consistency(ws, pid)
    assert not [a for a in alerts if a.rule_id == "R-TL"]


def test_rtime_reports_overdue(ws_factory):
    ws, pid = ws_factory()
    tl.add_pending(ws, pid, what="叶蓝出关", dt=1, vol=1, ch=1)
    st = worldstate.load(ws, pid)
    st["pending"][0]["overdue"] = 3
    worldstate.save(ws, pid, st)
    alerts = run_state_checks(ws, pid)
    rtime = [a for a in alerts if a.rule_id == "R-TIME"]
    assert rtime and "未兑现" in rtime[0].detail


def test_rstate_unavailable_appearance(ws_factory, write_json):
    """R-STATE：不可出场期人物在之后章节出场 → warn。"""
    ws, pid = ws_factory()
    _seed_chars(ws, pid, write_json)
    write_json(ws, pid, "bible/worldstate.json", {
        "time": {"now": 30, "origin_text": "开书之日"},
        "pending": [{"id": "pd:1", "who": "char:yelan", "what": "叶蓝闭关三月",
                     "due": 90, "span": 90, "status": "scheduled",
                     "created_at": {"vol": 1, "ch": 1}, "overdue": 0, "block_count": 0}],
        "characters": {"char:yelan": {"name": "叶蓝", "realm": "炼气三层",
                                      "unavailable_until": 90,
                                      "unavailable_since": {"vol": 1, "ch": 1},
                                      "unavailable_reason": "闭关"}},
    })
    # 第 2 章正文出现叶蓝 → warn；第 1 章（闭关起始章）不算
    ws.write_text(ws.outline_chapter_path(pid, 1, 2), "叶蓝在山中闭关。")  # 占位避免迭代异常
    ws.write_text(ws.draft_path(pid, 1, 2), "这一日，叶蓝竟出现在山门外。")
    alerts = run_state_checks(ws, pid)
    rst = [a for a in alerts if a.rule_id == "R-STATE" and "不可出场" in a.detail]
    assert rst, "闭关期间出场应被 R-STATE 告警"


def test_rtl_missing_location_warns(ws_factory, write_json):
    ws, pid = ws_factory()
    write_json(ws, pid, "bible/timeline.json", [{"id": "tl:1", "event": "无定位"}])
    alerts = run_consistency(ws, pid)
    assert any(a.rule_id == "R-TL" and "缺少" in a.detail for a in alerts)


def test_reminder_due_without_span_defaults(ws_factory):
    """旧数据无 span → 用 1 兜底（已到期必然强提示）。"""
    st = {"time": {"now": 100}, "pending": [{"id": "pd:1", "what": "旧日程",
                                             "due": 100, "status": "scheduled"}],
          "characters": {}}
    lines = tl.reminder_lines(st)
    assert any("宜安排" in ln for ln in lines)
