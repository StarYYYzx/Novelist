"""ADR-023（A2/A2b）+ C2 测试：实然关系账本 / 阈值升级提案 / 实然事件查询。

覆盖（docs/03 ADR-023 D2/D3-b + D1 补充，2026-09-03 定稿）：
- 账本聚合：视角 relations delta（新格式 方向·短语 与旧格式自由文本）→ 确定性聚合，
  同事件多视角方向归并（升温+降温 同场 → 转向 / diverged）；
- 阈值升级（D3-b）：同向 升温/降温 连续 ≥2 事件、或单次 断裂/复合 级 →
  enrich pending"关系修订提案"（人工 --allow，不改 bible；去重：bible 已同 / pending 已有）；
- 可再生：账本文件删除后可无 LLM 重建（ADR-016 投影）；
- N4 第 4 段词表解析兼容：record_perspectives 原样存 delta，"无"不产生增量；
- C2：query_recent_actual_events 时间序 + 参与者名回查；forge 章细纲 prompt 实然块。

单元测试绝不真调 LLM（docs/09 §2.1）——记录构造直写 character_histories 文件，
chronicler 路径用 stub_llm。
"""

from __future__ import annotations

import json

from novelist.core.chronicler import Chronicler
from novelist.core.memory import query_recent_actual_events
from novelist.core.rel_ledger import (
    enqueue_flip_proposals,
    ledger_lines_for,
    parse_delta,
    rebuild_ledger,
)
from novelist.forge import Blueprint
from novelist.forge.nodes import NodeContext, _chapter_prompt


def _seed_chars(ws, pid, write):
    write(ws, pid, "bible/characters.json", [
        {"id": "char:a", "name": "甲", "status": "active"},
        {"id": "char:b", "name": "乙", "status": "active"},
    ])


def _persp(ws, pid, cid, vol, ch, t, rels, ev_idx=1, event_ref=None):
    """直写一条 kind=perspective 视角条目（带 relations delta）。"""
    p = ws.char_history_path(pid, cid)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {
        "char_id": cid, "revision": 0, "entries": []}
    data["entries"].append({
        "at": {"vol": vol, "ch": ch, "t": t},
        "kind": "perspective",
        "relations": [{"who": other, "delta": delta} for other, delta in rels],
        "event_ref": event_ref or f"ev:proj:{vol}:{ch}:e{ev_idx}",
        "summary": f"[视角] v{vol}c{ch}t{t}",
    })
    data["revision"] += 1
    ws.write_json(p, data)


# ---------------------------------------------------------------- delta 解析


def test_parse_delta_new_vocab_prefix():
    assert parse_delta("升温·信任加深") == ("升温", "信任加深")
    assert parse_delta("降温：起了提防") == ("降温", "起了提防")
    assert parse_delta("断裂·当众翻脸") == ("断裂", "当众翻脸")
    assert parse_delta("断裂") == ("断裂", "断裂")  # 前缀无短语 → 方向词兜底
    assert parse_delta("") == ("不变", "")


def test_parse_delta_old_free_text_classifies():
    assert parse_delta("记恨加深") == ("降温", "记恨加深")
    assert parse_delta("信任加深") == ("升温", "信任加深")
    assert parse_delta("当众反目") == ("断裂", "当众反目")
    assert parse_delta("和解") == ("复合", "和解")
    assert parse_delta("心疼") == ("升温", "心疼")
    assert parse_delta("无关短语") == ("不变", "无关短语")


# ---------------------------------------------------------------- 聚合


def test_rebuild_aggregates_pair_state_and_by(ws_factory, write_json):
    ws, pid = ws_factory()
    _seed_chars(ws, pid, write_json)
    # A 视角 v1c1 升温；B 视角 v1c2 降温（跨事件）
    _persp(ws, pid, "char:a", 1, 1, 1, [("char:b", "升温·开始信任")], ev_idx=1)
    _persp(ws, pid, "char:b", 1, 2, 2, [("char:a", "降温·起了防备")], ev_idx=1)
    ledger = rebuild_ledger(ws, pid)
    assert (ledger.get("meta") or {}).get("count") == 1
    row = ledger["pairs"][0]
    assert row["pair"] in ("char:a|char:b", "char:b|char:a")
    # 状态 = 最新 delta 短语；趋势 = 最新事件方向
    assert row["state"] == "起了防备"
    assert row["trend"] == "降温"
    assert row["state_from"] == "char:b"
    assert row["last_event"].endswith(":e1")
    # 双方认知分开存
    assert set(row["by"]) == {"char:a", "char:b"}
    assert row["by"]["char:a"][0]["dir"] == "升温"
    assert row["by"]["char:b"][0]["dir"] == "降温"
    # 可再生投影：删文件重建幂等（无 LLM）
    ws._abs(f"{pid}/memory/relationship_ledger.json").unlink()  # noqa: SLF001
    again = rebuild_ledger(ws, pid)
    assert (again.get("meta") or {}).get("count") == 1
    assert again["pairs"][0]["state"] == "起了防备"


def test_diverged_when_views_conflict_same_event(ws_factory, write_json):
    """同事件 A 升温 / B 降温 → 方向归并 转向，diverged=True，不触发翻转提案。"""
    ws, pid = ws_factory()
    _seed_chars(ws, pid, write_json)
    _persp(ws, pid, "char:a", 1, 1, 1, [("char:b", "升温·更亲近")], ev_idx=1)
    _persp(ws, pid, "char:b", 1, 1, 1, [("char:a", "降温·生了警惕")], ev_idx=1)
    row = rebuild_ledger(ws, pid)["pairs"][0]
    assert row["trend"] == "转向"
    assert row["diverged"] is True
    assert row["flip"] is None
    assert enqueue_flip_proposals(ws, pid) == []


# ---------------------------------------------------------------- 阈值升级（D3-b）


def test_flip_consecutive_up_two_events_enqueues_proposal(ws_factory, write_json):
    ws, pid = ws_factory()
    _seed_chars(ws, pid, write_json)
    # A→B 同向升温连续 2 个事件（不同章）
    _persp(ws, pid, "char:a", 1, 1, 1, [("char:b", "升温·开始信任")], ev_idx=1)
    _persp(ws, pid, "char:a", 1, 2, 2, [("char:b", "升温·更信赖")], ev_idx=1)
    ledger = rebuild_ledger(ws, pid)
    row = ledger["pairs"][0]
    assert row["flip"] == "同向升温×2"
    added = enqueue_flip_proposals(ws, pid, ledger)
    assert len(added) == 1
    item = added[0]
    assert item["card_name"] == "甲"          # 无既有关系行 → 挂靠 a 侧
    assert item["relationships"] == [{"target": "乙", "type": "更信赖"}]
    assert "阈值升级" in item["reason"] and "同向升温×2" in item["reason"]
    # 落盘到 enrich pending（bible 下），bible 卡未被改动
    p = ws._abs(f"{pid}/bible/characters_enrich_pending.json")  # noqa: SLF001
    assert p.exists()
    chars = json.loads(ws._abs(f"{pid}/bible/characters.json").read_text(encoding="utf-8"))  # noqa: SLF001
    assert not (chars[0].get("relationships") or [])  # 提案不动应然层


def test_flip_single_break_level_triggers(ws_factory, write_json):
    ws, pid = ws_factory()
    _seed_chars(ws, pid, write_json)
    _persp(ws, pid, "char:a", 1, 3, 3, [("char:b", "断裂·当众翻脸")], ev_idx=1)
    ledger = rebuild_ledger(ws, pid)
    assert ledger["pairs"][0]["flip"] == "断裂级"
    added = enqueue_flip_proposals(ws, pid, ledger)
    assert len(added) == 1


def test_enqueue_dedupes_across_rebuilds_and_against_bible(ws_factory, write_json):
    ws, pid = ws_factory()
    _seed_chars(ws, pid, write_json)
    _persp(ws, pid, "char:a", 1, 1, 1, [("char:b", "升温·开始信任")], ev_idx=1)
    _persp(ws, pid, "char:a", 1, 2, 2, [("char:b", "升温·更信赖")], ev_idx=1)
    first = enqueue_flip_proposals(ws, pid)
    assert len(first) == 1
    # 幂等：再重建再入队 → 无新提案（pending 已有同 卡/target/短语）
    assert enqueue_flip_proposals(ws, pid) == []
    # bible 关系行已是该状态 → 不再提案（即便 pending 被清空）
    write_json(ws, pid, "bible/characters.json", [
        {"id": "char:a", "name": "甲", "status": "active",
         "relationships": [{"target": "char:b", "type": "更信赖"}]},
        {"id": "char:b", "name": "乙", "status": "active"},
    ])
    p = ws._abs(f"{pid}/bible/characters_enrich_pending.json")  # noqa: SLF001
    p.write_text(json.dumps([]), encoding="utf-8")
    assert enqueue_flip_proposals(ws, pid) == []


def test_ledger_lines_for_feeds_enrich_prior(ws_factory, write_json):
    ws, pid = ws_factory()
    _seed_chars(ws, pid, write_json)
    _persp(ws, pid, "char:a", 1, 1, 1, [("char:b", "升温·开始信任")], ev_idx=1)
    rebuild_ledger(ws, pid)
    lines = ledger_lines_for(ws, pid, "char:a")
    assert lines and "甲" in lines[0] and "乙" in lines[0] and "升温" in lines[0]
    assert ledger_lines_for(ws, pid, "char:a", limit=1) == lines[:1]


# ---------------------------------------------------------------- N4 词表解析兼容


def test_record_perspectives_vocab_col4_and_skips_wu(ws_factory, stub_llm, write_json):
    """新格式第 4 段（方向·短语）原样入库；"无"不产生增量；容忍"对"前缀。"""
    ws, pid = ws_factory()
    write_json(ws, pid, "bible/characters.json", [
        {"id": "char:yelan", "name": "叶岚"}, {"id": "char:sw", "name": "苏晚"}])
    reply = ("叶岚 | 受挫 | 记下了这笔账 | 无\n"
             "苏晚 | 担忧 | 想替他出头 | 对叶岚：升温·开始信任\n")
    c = Chronicler(ws, pid, llm=stub_llm(reply))
    assert c.record_perspectives("正文……", ["叶岚", "苏晚"], 1, 2, event_index=1) == 2
    hist = json.loads(ws.char_history_path(pid, "char:sw").read_text(encoding="utf-8"))
    rels = hist["entries"][-1]["relations"]
    assert rels == [{"who": "char:yelan", "delta": "升温·开始信任"}]
    hist_yl = json.loads(ws.char_history_path(pid, "char:yelan").read_text(encoding="utf-8"))
    assert hist_yl["entries"][-1].get("relations") == []  # "无"被丢弃


# ---------------------------------------------------------------- C2 实然事件查询


def test_query_recent_actual_events_time_ordered_with_names(ws_factory, write_json):
    ws, pid = ws_factory()
    write_json(ws, pid, "bible/characters.json", [
        {"id": "char:a", "name": "甲"}, {"id": "char:b", "name": "乙"}])
    evs = [
        {"id": "ev:1", "at": {"vol": 1, "ch": 1, "t": 3}, "type": "conflict",
         "summary": "甲与乙在藏经阁对峙", "participants": ["char:a", "char:b"]},
        {"id": "ev:2", "at": {"vol": 1, "ch": 1, "t": 1}, "type": "discovery",
         "summary": "乙发现断玉佩", "participants": ["char:b"]},
        {"id": "ev:3", "at": {"vol": 1, "ch": 1, "t": 2}, "type": "plot",
         "summary": "甲拜入师门", "participants": ["char:a"]},
    ]
    write_json(ws, pid, "memory/plot_events.json", evs)
    out = query_recent_actual_events(ws, pid)
    assert [e["summary"] for e in out] == ["乙发现断玉佩", "甲拜入师门", "甲与乙在藏经阁对峙"]
    assert out[-1]["participants"] == ["甲", "乙"]
    limited = query_recent_actual_events(ws, pid, limit=2)
    assert len(limited) == 2 and limited[-1]["summary"] == "甲与乙在藏经阁对峙"
    assert query_recent_actual_events(ws, pid + "-none") == []


def test_forge_chapter_prompt_injects_actual_events_block(ws_factory, write_json):
    """C2：章细纲 prompt 在计划态 prev 之外追加实然事件块；无正文记忆时原行为不变。"""
    ws, pid = ws_factory()
    bp = Blueprint.blank({"title": "t", "genre": "修仙", "logline": "x",
                          "scale": {"volumes": 1, "chapters_per_volume": 2,
                                    "target_words_per_chapter": 100}})
    ctx = NodeContext(ws=ws, project_id=pid, bp=bp, provider=None, pack={}, spec=None,
                      vol=1, ch=2)
    _, user = _chapter_prompt(ctx)
    assert "已落定实情" not in user            # 纯细纲期：无实然可读

    write_json(ws, pid, "bible/characters.json", [{"id": "char:a", "name": "甲"}])
    write_json(ws, pid, "memory/plot_events.json", [
        {"id": "ev:1", "at": {"vol": 1, "ch": 1, "t": 1}, "type": "conflict",
         "summary": "甲察觉杂役修为有异", "participants": ["char:a"]},
    ])
    _, user2 = _chapter_prompt(ctx)
    assert "已落定实情" in user2
    assert "- v1c1 甲察觉杂役修为有异（甲）" in user2
