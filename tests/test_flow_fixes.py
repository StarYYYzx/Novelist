"""流程异常排查修复（2026-09-05）单元测试。

对应 docs/流程异常排查-2026-09-05.md：
- F4/I1-I4：重跑幂等——timeline.advance/rollback、tick.overdue、motif、entity
- F2 修复相关：chronicler 头尾窗口（H2）
- F3：跨卷回读定位 _prev_chapter_loc
- G5：润色截断闸门（finish_reason=length / 篇幅 <70%）
- G6：revise 保护合并 _merge_protected_section
- H4：group_slots 顺延；H6：parse_round_line 越界告警
- H7：load_prev_facts 分节配额
- H8：build_system_prompt(event_loop=True) 不写标题
"""
from __future__ import annotations

import json

from novelist.core import timeline as tl
from novelist.core.chronicler import _chapter_window
from novelist.core.context import build_system_prompt
from novelist.core.entity import EntityTracker
from novelist.core.motif import MotifLedger
from novelist.core.orchestrator import _prev_chapter_loc
from novelist.core.polish import polish_chapter
from novelist.core.volume_facts import load_prev_facts
from novelist.forge.ask import RoundQuestion, parse_round_line
from novelist.forge.nodes import _merge_protected_section
from novelist.forge.slots import Slot, group_slots
from novelist.forge.state import Blueprint
from novelist.storage.workspace import Workspace


# ---------------------------------------------------------------- timeline（I1/I4）

def _ws(tmp_path):
    ws = Workspace(root=str(tmp_path))
    pid = "proj-t"
    ws.create_project(pid)
    return ws, pid


def test_advance_records_dt_and_rollback_rewinds(tmp_path):
    ws, pid = _ws(tmp_path)
    tl.advance(ws, pid, 30, vol=1, ch=1, event="闭关")
    tl.advance(ws, pid, 10, vol=1, ch=2)
    assert tl.now_of(tl._load(ws, pid)) == 40
    entries = tl.load_timeline(ws, pid)
    assert sum(int(e.get("dt") or 0) for e in entries) == 40
    stats = tl.rollback_chapter(ws, pid, 1, 1)
    assert stats["removed"] == 1 and stats["rewound"] == 30
    assert tl.now_of(tl._load(ws, pid)) == 10
    # 重跑同章：advance → rollback → advance 结果一致（幂等）
    tl.advance(ws, pid, 30, vol=1, ch=1)
    assert tl.now_of(tl._load(ws, pid)) == 40


def test_tick_overdue_not_double_counted_per_chapter(tmp_path):
    ws, pid = _ws(tmp_path)
    tl.add_pending(ws, pid, what="叶岚出关", dt=5, vol=1, ch=1, who="char:yelan")
    # 时间推进到 due 之后（第 1 章末 +10 天）
    tl.advance(ws, pid, 10, vol=1, ch=1)
    # 第 2 章结束时已到期 → overdue=1（od=1 无 warn）
    tl.tick(ws, pid, vol=1, ch=2, chapter_text="本章什么都没发生。")
    state = tl._load(ws, pid)
    p = tl.pending_of(state)[0]
    assert int(p["overdue"]) == 1
    # 重跑第 2 章（同一章再 tick 一次）不得再递增
    tl.tick(ws, pid, vol=1, ch=2, chapter_text="本章什么都没发生。")
    state = tl._load(ws, pid)
    assert int(tl.pending_of(state)[0]["overdue"]) == 1
    # 第 3 章 → 正常递增（正文不得命中"出关"以免误判兑现）
    tl.tick(ws, pid, vol=1, ch=3, chapter_text="平淡无奇的一章。")
    state = tl._load(ws, pid)
    assert int(tl.pending_of(state)[0]["overdue"]) == 2


# ---------------------------------------------------------------- motif（I2）

def test_motif_add_text_idempotent_on_rerun(tmp_path):
    ws, pid = _ws(tmp_path)
    led = MotifLedger()
    text = "他指尖轻划虚空，一道符光闪过。"
    # 生产链路语义：orchestrator 先 remove_chapter 再 add_text（I2）
    led.add_text(text, 5)
    assert led.entries[0]["count"] == 1
    led.remove_chapter(5)
    led.add_text(text, 5)  # 重跑：替换而非累计
    assert led.entries[0]["count"] == 1
    assert led.entries[0]["chapters"] == [5]
    # 跨章同母题：累计
    led.add_text(text, 6)
    assert led.entries[0]["count"] == 2
    assert sorted(led.entries[0]["chapters"]) == [5, 6]
    # 再重跑第 6 章：仍为 2（不翻倍、禁令不虚增）
    led.remove_chapter(6)
    led.add_text(text, 6)
    assert led.entries[0]["count"] == 2


# ---------------------------------------------------------------- entity（I3）

def _seed_entities(ws, pid):
    ws.write_json(ws._abs(f"{pid}/bible/characters.json"), [
        {"id": "char:p", "name": "路人甲", "gender": "male", "core_traits": []}])
    ws.write_json(ws._abs(f"{pid}/bible/worldview.json"), {"name": "界"})


def test_entity_rerun_same_chapter_replaces_mentions(tmp_path):
    ws, pid = _ws(tmp_path)
    _seed_entities(ws, pid)
    t = EntityTracker.load(ws, pid)
    t.update_from_chapter("路人甲。" * 4, 1, 1)  # 4 次
    assert t.entities["char:p"].mentions == 4
    # 重跑同章，正文变化 → 替换而非累加
    t.update_from_chapter("路人甲。", 1, 1)  # 1 次
    assert t.entities["char:p"].mentions == 1
    # 不同章照常累加
    t.update_from_chapter("路人甲又来了。", 1, 2)
    assert t.entities["char:p"].mentions == 2


# ---------------------------------------------------------------- chronicler（H2）

def test_chapter_window_keeps_head_and_tail():
    text = "A" * 500 + "B" * 3000 + "C" * 2000
    win = _chapter_window(text, 2500)
    assert len(win) < 2600
    assert win.startswith("A")      # 头部保留
    assert win.rstrip().endswith("C")  # 尾部保留
    assert "中段略" in win
    assert _chapter_window("短文本", 2500) == "短文本"


# ---------------------------------------------------------------- orchestrator（F3）

def test_prev_chapter_loc_cross_volume(tmp_path):
    ws, pid = _ws(tmp_path)
    # 第 1 卷有 2 章草稿，第 2 卷还没写
    for ch in (1, 2):
        p = ws.draft_path(pid, 1, ch)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"第{ch}章正文", encoding="utf-8")
    assert _prev_chapter_loc(ws, pid, 1, 3) == (1, 2)
    assert _prev_chapter_loc(ws, pid, 2, 1) == (1, 2)  # 跨卷回退到前卷末章
    assert _prev_chapter_loc(ws, pid, 1, 1) is None    # 全书第一章


# ---------------------------------------------------------------- polish（G5）

class _StubLLM:
    def __init__(self, reply: str, finish_reason: str = "stop"):
        self._reply = reply
        self._fr = finish_reason

    def complete(self, req):
        from novelist.core.llm import LLMResult

        return LLMResult(ok=True, content=self._reply, finish_reason=self._fr)


def _long_text(n_sentences: int = 60) -> str:
    return "。" .join(f"第{i}句，他沿着山道走了片刻" for i in range(n_sentences)) + "。"


def test_polish_rejects_short_output(tmp_path=None):
    text = _long_text()
    short = _long_text(30)  # ~50% 篇幅，句号结尾
    res = polish_chapter(text, _StubLLM(short))
    assert not res.changed
    assert "kept original" in res.note


def test_polish_rejects_finish_reason_length():
    text = _long_text()
    res = polish_chapter(text, _StubLLM(_long_text(80), finish_reason="length"))
    assert not res.changed
    assert "kept original" in res.note


# ---------------------------------------------------------------- nodes（G6）

def test_merge_protected_section_dict_and_list():
    bp = Blueprint(data={"characters": []} if False else {})
    # dict 段
    bp.data["style"] = {"tone": ["冷肃"], "pov": "third"}
    bp.set_provenance("style.tone", "user", 1.0)
    out = _merge_protected_section(bp, "style", bp.data["style"],
                                   {"tone": ["热络"], "pov": "first"})
    assert out["tone"] == ["冷肃"]      # user 保护字段不被覆盖
    assert out["pov"] == "first"
    # list 段：逐字段保护 + 旧条目保留
    bp.data["characters"] = [{"id": "char:a", "name": "旧名", "role": "rival"}]
    bp.set_provenance("characters[char:a].name", "user", 1.0)
    out2 = _merge_protected_section(bp, "characters", bp.data["characters"],
                                    [{"id": "char:a", "name": "新名", "role": "ally"}])
    assert len(out2) == 1
    assert out2[0]["name"] == "旧名"
    assert out2[0]["role"] == "ally"


# ---------------------------------------------------------------- ask/slots（H4/H6）

def test_group_slots_overflow_extends_rounds():
    slots = [Slot(key=f"k{i}", label=f"q{i}", level="required", group=1)
             for i in range(5)]
    rounds = group_slots(slots, per_round=4)
    flat = [s.key for _, chunk in rounds for s in chunk]
    assert flat == [f"k{i}" for i in range(5)]  # 第 5 个顺延，不再被丢弃


def test_parse_round_line_out_of_range_warns():
    q = RoundQuestion(slot=Slot(key="a", label="q", level="required", group=1),
                      candidates=["x", "y"], default="推荐")
    ans = parse_round_line("5 我想要的答案", [q])
    assert ans.note  # 显式告警，不再静默
    assert ans.values["a"].value == "推荐"


# ---------------------------------------------------------------- volume_facts（H7）

def test_load_prev_facts_keeps_all_sections(tmp_path):
    ws, pid = _ws(tmp_path)
    body = (
        "# 第 1 卷末事实清单\n\n"
        "## 人物状态\n- 叶岚：炼气三层\n\n"
        "## 时间线\n- 推进 90 天\n\n"
        "## 未回收伏笔\n- [第3章] 神秘令牌\n\n"
        "## 未决冲突\n- 与血煞门的死仇\n")
    ws.write_text(ws._abs(f"{pid}/workspace/forge/volume-facts-v1.md"), body)
    out = load_prev_facts(ws, pid, 2, max_chars=100)
    assert "未回收伏笔" in out   # 分节配额：末尾两节不再被 head-only 切掉
    assert "未决冲突" in out
    assert len(out) <= 100


# ---------------------------------------------------------------- context（H8）

def test_system_prompt_event_loop_no_title():
    bible = {"worldview": {"name": "界", "power_system": {"levels": ["练气"]}},
             "style": {"tone": ["冷肃"]}, "characters": [], "volumes": []}
    cast = []
    sp_direct = build_system_prompt(bible, cast, 1, 1, event_loop=False)
    sp_event = build_system_prompt(bible, cast, 1, 1, event_loop=True)
    assert "第一行是章节标题" in sp_direct
    assert "第一行是章节标题" not in sp_event
    assert "不要写章节标题" in sp_event
