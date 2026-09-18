"""M5 高级生成特性测试（人工审查第二、三批落地）。

- 事件循环：声明式 key_events 逐事件推进、逐事件回写（ADR-013 真落地）
- 续写：finish_reason=length 时续写而非整章重来
- 剧本草稿：重场戏先剧本体后叙事化
- 世界状态层（worldstate）+ R-STATE：境界单调性/越级跳变/阵亡复核
- 细纲 key_events 解析与状态注入生成上下文
"""

from __future__ import annotations

import json


from novelist.consistency.rules import run_rule_checks, run_state_checks
from novelist.core.chronicler import Chronicler
from novelist.core.context import build_chapter_context, parse_key_events
from novelist.core.llm import LLMResult
from novelist.core.orchestrator import produce_chapter, _generate_with_continuation
from novelist.core.output import use_output
from novelist.core.session import SessionInfo
from novelist.core.worldstate import (
    apply_delta,
    init_from_bible,
    parse_state_lines,
    snapshot_lines,
)
from novelist.storage.checkpoint import Checkpoint
from novelist.storage.workspace import Workspace


def _project(tmp_path, pid="proj-m5"):
    ws = Workspace(root=str(tmp_path))
    ws.create_project(pid)
    Checkpoint(ws).save(pid, {"id": pid, "title": "t", "pipeline_state": "正文", "event_seq": 0})
    return ws, pid


def _write(ws, pid, rel, data):
    ws.write_json(ws._abs(f"{pid}/{rel}"), data)


def _seed(ws, pid, *, with_worldview=True):
    _write(ws, pid, "bible/characters.json", [
        {"id": "char:lf", "name": "苏晚", "gender": "male", "status": "active",
         "core_traits": ["隐忍"], "power": {"level": "炼气三层", "faction": "青云宗"},
         "first_appear": {"vol": 1, "ch": 1}, "is_protagonist": True},
        {"id": "char:twy", "name": "铁无涯", "gender": "male", "status": "active",
         "power": {"level": "金丹初期", "faction": "青云宗"}, "first_appear": {"vol": 1, "ch": 1}},
        {"id": "char:zh", "name": "赵虎", "gender": "male", "status": "active",
         "power": {"level": "炼气五层", "faction": "青云宗"}, "first_appear": {"vol": 1, "ch": 2}},
    ])
    _write(ws, pid, "bible/style.json", {
        "protagonist": {"id": "char:lf", "name": "苏晚", "gender": "male"},
        "pov": "第三人称限知", "tone": ["冷峻"], "forbidden_words": ["签到"],
        "target_words_per_chapter": 800})
    if with_worldview:
        _write(ws, pid, "bible/worldview.json", {
            "name": "青冥界", "power_system": {"levels": ["炼气", "筑基", "金丹", "元婴"]},
            "rules": ["修士不可对凡人出手"]})


class _SeqLLM:
    """按脚本顺序返回 LLMResult 的替身（不调真实模型）。"""

    def __init__(self, responses: list[LLMResult]) -> None:
        self.responses = list(responses)
        self.calls = 0

    def complete(self, req):
        assert self.responses, "script exhausted"
        self.calls += 1
        return self.responses.pop(0)


def _res(content: str, finish: str = "stop") -> LLMResult:
    return LLMResult(ok=True, content=content, finish_reason=finish, provider="stub")


# ---------------------------------------------------------------- 世界状态层


def test_worldstate_init_and_apply(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    init_from_bible(ws, pid)
    st = json.loads(ws._abs(f"{pid}/bible/worldstate.json").read_text(encoding="utf-8"))
    assert st["characters"]["char:lf"]["realm"] == "炼气三层", "从人物卡 power.level 初始化"

    applied = apply_delta(ws, pid, "char:lf",
                          {"修为": "炼气四层", "位置": "藏经阁", "获得": "青冥诀残篇", "受伤": "经脉灼伤"},
                          at={"vol": 1, "ch": 2})
    assert applied["realm"] == "炼气四层"
    cur = st["characters"]["char:lf"]
    cur = json.loads(ws._abs(f"{pid}/bible/worldstate.json").read_text(encoding="utf-8"))["characters"]["char:lf"]
    assert cur["realm"] == "炼气四层"
    assert cur["location"] == "藏经阁"
    assert cur["items"] == ["青冥诀残篇"]
    assert cur["injuries"] == ["经脉灼伤"]
    assert len(cur["history"]) == 1

    # 失去与痊愈
    apply_delta(ws, pid, "char:lf", {"失去": "青冥诀残篇", "痊愈": "经脉灼伤"}, at={"vol": 1, "ch": 3})
    cur = json.loads(ws._abs(f"{pid}/bible/worldstate.json").read_text(encoding="utf-8"))["characters"]["char:lf"]
    assert cur["items"] == [] and cur["injuries"] == []


def test_worldstate_ignores_unknown_chars(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    init_from_bible(ws, pid)
    apply_delta(ws, pid, "char:nobody", {"修为": "金丹"}, at={"vol": 1, "ch": 1})
    st = json.loads(ws._abs(f"{pid}/bible/worldstate.json").read_text(encoding="utf-8"))
    assert "char:nobody" in st["characters"], "未知人物也允许登记（宽容），但需有 name"


def test_worldstate_snapshot_lines(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    init_from_bible(ws, pid)
    apply_delta(ws, pid, "char:lf", {"修为": "炼气四层", "位置": "藏经阁", "获得": "残篇"}, at={"vol": 1, "ch": 1})
    lines = snapshot_lines(json.loads(ws._abs(f"{pid}/bible/worldstate.json").read_text(encoding="utf-8")),
                           ["char:lf"])
    joined = " ".join(lines)
    assert "炼气四层" in joined and "藏经阁" in joined and "残篇" in joined


def test_parse_state_lines():
    name_map = {"苏晚": "char:lf", "赵虎": "char:zh"}
    changes = parse_state_lines(
        "状态：苏晚 | 修为：炼气四层 | 位置：藏经阁 | 获得：残篇\n状态：赵虎 | 受伤：骨折\n",
        name_map)
    assert changes[0] == ("char:lf", {"修为": "炼气四层", "位置": "藏经阁", "获得": "残篇"})
    assert changes[1] == ("char:zh", {"受伤": "骨折"})


# ---------------------------------------------------------------- R-STATE


def _seed_state_history(ws, pid):
    _seed(ws, pid)
    init_from_bible(ws, pid)
    apply_delta(ws, pid, "char:lf", {"修为": "炼气三层"}, at={"vol": 1, "ch": 1})
    return ws


def test_rstate_blocks_realm_regression(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    init_from_bible(ws, pid)
    apply_delta(ws, pid, "char:lf", {"修为": "炼气三层"}, at={"vol": 1, "ch": 1})
    apply_delta(ws, pid, "char:lf", {"修为": "炼气二层"}, at={"vol": 1, "ch": 2})  # 倒退
    alerts = [a for a in run_state_checks(ws, pid) if a.rule_id == "R-STATE"]
    assert any(a.level == "block" and "倒退" in a.detail for a in alerts)


def test_rstate_warns_on_level_skip(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    init_from_bible(ws, pid)
    apply_delta(ws, pid, "char:zh", {"修为": "炼气五层"}, at={"vol": 1, "ch": 2})
    apply_delta(ws, pid, "char:zh", {"修为": "金丹中期"}, at={"vol": 1, "ch": 3})  # 跨 2 个大境界
    alerts = [a for a in run_state_checks(ws, pid) if a.rule_id == "R-STATE"]
    assert any(a.level == "warn" and "越级跳变" in a.detail for a in alerts)


def test_rstate_warns_when_dead_character_appears_later(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    init_from_bible(ws, pid)
    apply_delta(ws, pid, "char:zh", {"阵亡": "是"}, at={"vol": 1, "ch": 1})
    d = ws._abs(f"{pid}/chapters")
    d.mkdir(parents=True, exist_ok=True)
    (d / "1-2.md").write_text("赵虎从阴影里走了出来。", encoding="utf-8")
    alerts = [a for a in run_state_checks(ws, pid) if a.rule_id == "R-STATE"]
    assert any(a.level == "warn" and "阵亡" in a.detail for a in alerts)


def test_rstate_clean_when_monotonic(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    init_from_bible(ws, pid)
    apply_delta(ws, pid, "char:lf", {"修为": "炼气三层"}, at={"vol": 1, "ch": 1})
    apply_delta(ws, pid, "char:lf", {"修为": "筑基初期"}, at={"vol": 1, "ch": 3})
    assert not [a for a in run_state_checks(ws, pid) if a.rule_id == "R-STATE"]


def test_run_rule_checks_includes_state(tmp_path):
    """R-STATE 必须纳入 `run_rule_checks`（全量规则入口）。

    2026-09-18：此例原先用 MagicMock 造了个 ws 就断言 `callable(run_rule_checks)`——
    恒真、且**从未调用**被测函数，规则从全量入口里掉出去也不会红。改为真实跑一遍。
    """
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    init_from_bible(ws, pid)
    apply_delta(ws, pid, "char:lf", {"修为": "炼气三层"}, at={"vol": 1, "ch": 1})
    apply_delta(ws, pid, "char:lf", {"修为": "炼气二层"}, at={"vol": 1, "ch": 2})  # 倒退

    all_alerts = run_rule_checks(ws, pid)
    ids = {a.rule_id for a in all_alerts}
    assert "R-STATE" in ids, f"全量入口漏掉 R-STATE，实际只有 {sorted(ids)}"

    # 全量入口应当是"只跑状态规则"的超集（同一条告警都要在）
    state_ids = {(a.rule_id, a.object_ref, a.detail)
                 for a in run_state_checks(ws, pid) if a.rule_id == "R-STATE"}
    all_ids = {(a.rule_id, a.object_ref, a.detail)
               for a in all_alerts if a.rule_id == "R-STATE"}
    assert state_ids and state_ids <= all_ids
    assert any(a.level == "block" and "倒退" in a.detail for a in all_alerts)


def test_run_rule_checks_clean_project_still_returns_list(tmp_path):
    """干净项目：全量入口不炸、返回列表（防"规则全掉光"也算绿）。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    alerts = run_rule_checks(ws, pid)
    assert isinstance(alerts, list)


# ---------------------------------------------------------------- key_events 解析与状态注入


def test_parse_key_events():
    gist = "---\nid: ch:1:1\nvol: 1\nch: 1\ntitle: 弃徒\nkey_events: [苏晚被逐出内门, 拾得断玉佩]\n---\n\n正文"
    assert parse_key_events(gist) == ["苏晚被逐出内门", "拾得断玉佩"]
    assert parse_key_events("## 无 front-matter") == []
    assert parse_key_events("key_events: []") == []


def test_context_injects_worldstate(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    init_from_bible(ws, pid)
    apply_delta(ws, pid, "char:lf", {"修为": "炼气四层", "位置": "藏经阁", "获得": "残篇"}, at={"vol": 1, "ch": 1})
    sp = build_chapter_context(ws, pid, 1, 2).system_prompt
    assert "人物当前状态" not in sp, "RAG 化后状态不再全量进 system（讨论第 8 轮）"
    from novelist.core.knowledge import KnowledgeBase
    kb = KnowledgeBase(ws, pid)
    ch_items = [it for it in kb._items if it.kind == "character" and "苏晚" in it.text]
    assert ch_items and "炼气四层" in ch_items[0].payload.get("state", ""), "状态随人物卡事件级注入"


# ---------------------------------------------------------------- 编纂员状态回写


def test_chronicler_updates_worldstate(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    init_from_bible(ws, pid)
    llm = _SeqLLM([
        _res("苏晚拾得断玉佩 | discovery | 苏晚\n状态：苏晚 | 修为：炼气四层 | 位置：藏经阁"),
    ])
    report = Chronicler(ws, pid, llm=llm).run("正文略", 1, 1)
    assert report.written == 1
    assert report.state_updates.get("char:lf", {}).get("realm") == "炼气四层"
    st = json.loads(ws._abs(f"{pid}/bible/worldstate.json").read_text(encoding="utf-8"))
    assert st["characters"]["char:lf"]["realm"] == "炼气四层"
    # state_delta 也写进人物经历（docs/06 §3.5 预留字段）
    hist = json.loads(ws.char_history_path(pid, "char:lf").read_text(encoding="utf-8"))
    assert hist["entries"][-1].get("state_delta", {}).get("修为") == "炼气四层"


# ---------------------------------------------------------------- 事件循环


def test_event_loop_generates_per_event_and_writes_back(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    gist = ws.outline_chapter_path(pid, 1, 1)
    gist.parent.mkdir(parents=True, exist_ok=True)
    gist.write_text(
        "---\nvol: 1\nch: 1\ntitle: 弃徒\n"
        "key_events: [苏晚被逐出内门, 下山拾得断玉佩]\n---\n\n正文要点",
        encoding="utf-8")

    llm = _SeqLLM([
        _res("苏晚被逐出内门，他叩首退下，一言不发地走下高台。"),  # gen event1
        _res("ok"),                                               # 审校 event1
        _res("苏晚被逐 | conflict | 苏晚"),                       # chronicler1
        _res("下山时拾得半枚焦黑玉佩，掌心发烫，他知道这不简单。"),  # gen event2
        _res("ok"),                                               # 审校 event2
        _res("拾得玉佩 | discovery | 苏晚"),                     # chronicler2
    ])
    res = produce_chapter(
        ws, pid, 1, 1, llm, prefer_direct=True,
        inject_bible=False, event_loop=True, commit_chapter_event=False,
        knowledge_llm=False, session=SessionInfo(project_id=pid, agent="t"),
        # ADR-020 四件套默认开 + 广播默认开：本测试脚本队列不含其额外调用，显式关闭
        broadcast_casting=False,
        character_direction=False, perspective_memory=False, defer_title=False)
    assert res.ok, res.result
    final = ws.draft_path(pid, 1, 1).read_text(encoding="utf-8")
    assert "逐出内门" in final and "焦黑玉佩" in final
    assert res.chronicle is not None and res.chronicle.written >= 2, "逐事件回写必须发生"
    events = json.loads(ws._abs(f"{pid}/memory/plot_events.json").read_text(encoding="utf-8"))
    assert len(events) >= 2


def test_event_loop_emits_progress_with_event_index_and_elapsed(tmp_path):
    """U6（2026-09-15）：事件循环必须逐事件报进度（`事件 i/N 生成中…（已 mm:ss）`）。

    一次 chapter 是几十次 LLM 调用、几分钟起；原先全程近乎静默，用户分不清"在跑"还是"卡死"。
    用 sink 捕获 `emit` 输出，不依赖终端。
    """
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    gist = ws.outline_chapter_path(pid, 1, 1)
    gist.parent.mkdir(parents=True, exist_ok=True)
    gist.write_text(
        "---\nvol: 1\nch: 1\ntitle: 弃徒\n"
        "key_events: [苏晚被逐出内门, 下山拾得断玉佩]\n---\n\n正文要点",
        encoding="utf-8")
    llm = _SeqLLM([
        _res("苏晚被逐出内门，他叩首退下，一言不发地走下高台。"),
        _res("ok"),
        _res("苏晚被逐 | conflict | 苏晚"),
        _res("下山时拾得半枚焦黑玉佩，掌心发烫，他知道这不简单。"),
        _res("ok"),
        _res("拾得玉佩 | discovery | 苏晚"),
    ])
    lines: list[str] = []
    with use_output(lines.append):
        res = produce_chapter(
            ws, pid, 1, 1, llm, prefer_direct=True,
            inject_bible=False, event_loop=True, commit_chapter_event=False,
            knowledge_llm=False, session=SessionInfo(project_id=pid, agent="t"),
            broadcast_casting=False,
            character_direction=False, perspective_memory=False, defer_title=False)
    assert res.ok, res.result
    progress = [ln for ln in lines if "生成中" in ln]
    assert len(progress) == 2, f"应逐事件各报一次进度，实得：{progress}"
    assert "事件 1/2" in progress[0] and "事件 2/2" in progress[1]
    assert all("已 " in ln for ln in progress), f"进度须带已耗时长：{progress}"


def test_event_loop_without_key_events_falls_back_to_single_shot(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    llm = _SeqLLM([_res("整章直出的正文内容，这里写得完整且正确收束。")])
    res = produce_chapter(ws, pid, 1, 1, llm, prefer_direct=True,
                          inject_bible=False, event_loop=True,
                          commit_chapter_event=False,
                          session=SessionInfo(project_id=pid, agent="t"),
                          character_direction=False, perspective_memory=False,
                          defer_title=False)
    assert res.ok and "整章直出" in ws.draft_path(pid, 1, 1).read_text(encoding="utf-8")


# ---------------------------------------------------------------- 续写与剧本草稿


def test_continuation_appends_when_truncated():
    llm = _SeqLLM([
        _res("他走到山门前，正要", finish="length"),
        _res("踏入那扇朱红大门。", finish="stop"),
    ])
    text = _generate_with_continuation(llm, "你是主编剧。", "请写。", 100, 2)
    assert text == "他走到山门前，正要踏入那扇朱红大门。"
    assert llm.calls == 2


def test_continuation_stops_when_content_is_empty():
    llm = _SeqLLM([_res("正文。", finish="stop")])
    text = _generate_with_continuation(llm, "", "请写。", 100, 2)
    assert text == "正文。"
    assert llm.calls == 1


def test_screenplay_two_pass(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    llm = _SeqLLM([
        _res("铁无涯：（冷冷地）末位者，逐出内门。\n苏晚：（叩首）弟子领命。"),
        _res("## 第一章 弃徒\n\n铁无涯长老立在台上，令牌一扬：“末位者，逐出内门。”\n\n苏晚跪地，额头触地。"),
    ])
    res = produce_chapter(ws, pid, 1, 1, llm, prefer_direct=True,
                          inject_bible=False, screenplay=True,
                          commit_chapter_event=False,
                          session=SessionInfo(project_id=pid, agent="t"))
    assert res.ok
    final = ws.draft_path(pid, 1, 1).read_text(encoding="utf-8")
    assert "逐出内门" in final and "剧本" not in final, "叙事化后不应保留剧本格式"
