"""方案4（质量加固 2026-09-05）：章纲事件母题去重闸。

- event_dup_violations：整条包含 / 字符 Jaccard>0.5 判重；
- planned_events_ledger：本卷前序章 + 上一卷尾 2 章；
- _chapter_prompt 注入账本与拒绝重生成说明；
- _apply_chapter 首稿重复 → ValueError 触发引擎重试；重试仍重复 → 降级接受+告警。
"""

from __future__ import annotations

import pytest

from novelist.forge import Blueprint
from novelist.forge.nodes import (NodeContext, _apply_chapter, _chapter_prompt,
                                  event_dup_violations, planned_events_ledger,
                                  planned_titles)
from novelist.providers.fake import FakeProvider


def _bp():
    return Blueprint.blank({"title": "测试书", "genre": "修仙", "logline": "一句话",
                            "scale": {"volumes": 2, "chapters_per_volume": 5,
                                      "target_words_per_chapter": 2400}})


def _add_gist(bp, vol, ch, title, events):
    # chapters 行无 id（以 vol/ch 定位）——直接 append，绕过 upsert 的 id 去重语义
    bp.data.setdefault("chapters", []).append(
        {"vol": vol, "ch": ch, "title": title,
         "key_events": list(events), "characters": [],
         "threads_involved": [], "after_days": 0})


# ---- event_dup_violations ----

def test_dup_exact_and_containment():
    ledger = ["主角在图书馆发现古籍残卷，从中悟出聚灵阵"]
    assert event_dup_violations(["主角在图书馆发现古籍残卷，从中悟出聚灵阵"], ledger) == \
        ["主角在图书馆发现古籍残卷，从中悟出聚灵阵"]
    # 归一后包含（仅标点/语序微差）
    assert event_dup_violations(["主角在图书馆发现古籍残卷"], ledger) != []


def test_dup_jaccard_high_overlap():
    ledger = ["主角在图书馆发现古籍残卷并借机悟出聚灵阵的运转方式"]
    similar = "主角在图书馆发现古籍残卷并借机悟出聚灵阵的运行方式"
    assert event_dup_violations([similar], ledger) == [similar]


def test_no_dup_distinct_event():
    ledger = ["主角在图书馆发现古籍残卷，从中悟出聚灵阵"]
    assert event_dup_violations(["主角在武馆当众击败王傲天"], ledger) == []


# ---- planned_events_ledger / planned_titles ----

def test_ledger_same_volume_and_prev_volume_tail():
    bp = _bp()
    _add_gist(bp, 1, 1, "开局", ["事件一"])
    _add_gist(bp, 1, 4, "推进", ["事件四"])
    _add_gist(bp, 1, 5, "高潮", ["事件五"])
    _add_gist(bp, 1, 2, "发展", ["事件二"])
    led3 = planned_events_ledger(bp, 1, 3)
    assert "事件一" in led3 and "事件二" in led3
    assert "事件四" not in led3 and "事件五" not in led3  # 同卷只取前序章
    # 上一卷（vol=2 ch=1）：只取 vol1 最后 2 章（ch4、ch5）
    led_next = planned_events_ledger(bp, 2, 1)
    assert "事件四" in led_next and "事件五" in led_next
    assert "事件一" not in led_next


def test_planned_titles_recent_two():
    bp = _bp()
    _add_gist(bp, 1, 1, "代码重构修仙", ["事件一"])
    _add_gist(bp, 1, 2, "代码重构灵力", ["事件二"])
    _add_gist(bp, 1, 3, "天台夜话", ["事件三"])
    assert planned_titles(bp, 1, 4) == ["代码重构灵力", "天台夜话"]


# ---- prompt 注入 ----

def test_chapter_prompt_injects_ledger_and_rules(ws_factory):
    ws, pid = ws_factory("proj-dedup")
    bp = _bp()
    _add_gist(bp, 1, 1, "代码重构修仙", ["主角在图书馆发现古籍残卷"])
    ctx = NodeContext(ws=ws, project_id=pid, bp=bp, provider=FakeProvider(reply="x"),
                      pack={}, vol=1, ch=2)
    _sys, user = _chapter_prompt(ctx)
    assert "已规划事件账本" in user
    assert "严禁与【已规划事件账本】" in user
    assert "因果承接" in user
    assert "标题模板" in user  # 近两章标题句式禁复读
    assert "主角在图书馆发现古籍残卷" in user


def test_chapter_prompt_injects_reject_note(ws_factory):
    ws, pid = ws_factory("proj-dedup2")
    bp = _bp()
    ctx = NodeContext(ws=ws, project_id=pid, bp=bp, provider=FakeProvider(reply="x"),
                      pack={}, vol=1, ch=1, reject_note="主角在图书馆发现古籍残卷")
    _sys, user = _chapter_prompt(ctx)
    assert "上一稿被拒" in user and "全新" in user


# ---- apply 闸门 ----

def _ctx(ws, pid, bp):
    return NodeContext(ws=ws, project_id=pid, bp=bp, provider=FakeProvider(reply="x"),
                       pack={}, vol=1, ch=2)


def _node(events):
    return {"artifact": {"title": "古籍残卷", "key_events": list(events), "turns": [],
                         "characters": [], "threads_involved": [], "after_days": 0}}


def test_apply_chapter_rejects_dup_then_degrades(ws_factory):
    ws, pid = ws_factory("proj-gate")
    bp = _bp()
    _add_gist(bp, 1, 1, "开局", ["主角在图书馆发现古籍残卷"])
    ctx = _ctx(ws, pid, bp)
    with pytest.raises(ValueError, match="与已规划事件重复"):
        _apply_chapter(ctx, _node(["主角在图书馆发现古籍残卷"]))
    assert ctx.reject_note  # 拒绝原因已记，供重生成 prompt 引用
    # 重试（reject_note 已设）→ 降级接受 + 告警
    warns = _apply_chapter(ctx, _node(["主角在图书馆发现古籍残卷"]))
    assert any("降级接受" in w for w in warns)
    # 成功 apply 后 reject_note 清空
    ctx2 = _ctx(ws, pid, bp)
    warns2 = _apply_chapter(ctx2, _node(["主角在武馆当众击败王傲天"]))
    assert ctx2.reject_note == ""
    assert not any("重复" in w for w in warns2)
