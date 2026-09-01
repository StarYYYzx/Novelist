"""M3m T4 测试（docs/08）：Forge 联动——细纲 after_days 登记 pending + ingest 约定对齐。

覆盖：
- synthesize_worldstate：细纲章 gist 可选 `after_days` → worldstate.pending
  确定性登记（due = 此前章 after_days 累计，相对天数轴 now 从 0 起，ADR-019）；
- 无 after_days（ingest 场景）→ pending 为空（兼容）；
- 登记条目字段与 timeline.add_pending 对齐、通过 worldstate schema 校验；
- ingest 侧「约定：」抽取登记条目 status=scheduled（F3 曾误写 "pending"，
  timeline 下游 tick/软 block 只认 scheduled）。

单元测试绝不真调 LLM（docs/09 §2.1）。
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from novelist.core.timeline import parse_pending_line
from novelist.forge.nodes import synthesize_worldstate
from novelist.forge.state import Blueprint

_SCHEMAS = Path(__file__).resolve().parent.parent / "schemas"


def _gist(vol: int, ch: int, *, title: str = "", events: list[str] | None = None,
          after_days: int | None = None, done: bool = False) -> dict:
    g: dict = {"id": f"ch:{vol}:{ch}", "vol": vol, "ch": ch,
               "title": title or f"第 {ch} 章",
               "key_events": events or [], "done": done}
    if after_days is not None:
        g["after_days"] = after_days
    return g


# ---------------------------------------------------- after_days → pending 登记


def test_synthesize_pending_from_after_days():
    """after_days>0 的章 → pending 登记，due=此前累计（0/90/3 → 90/93）。"""
    bp = Blueprint.blank()
    bp.upsert("chapters", _gist(1, 1, events=["叶蓝入宗"], after_days=0))
    bp.upsert("chapters", _gist(1, 2, events=["宗门大比开启"], after_days=90))
    bp.upsert("chapters", _gist(1, 3, events=["叶蓝出关"], after_days=3))

    ws = synthesize_worldstate(bp)
    assert ws["time"]["now"] == 0
    pend = ws["pending"]
    assert [p["id"] for p in pend] == ["pd:ke-1-2", "pd:ke-1-3"]
    assert [p["due"] for p in pend] == [90, 93]           # 累计：90、90+3
    assert pend[0]["what"] == "宗门大比开启"               # 取首个 key_event
    assert all(p["status"] == "scheduled" for p in pend)
    assert all(p["span"] == p["due"] for p in pend)       # 登记时 now=0 → 跨度=due
    assert pend[0]["created_at"] == {"vol": 1, "ch": 2}


def test_synthesize_pending_what_falls_back_to_title():
    """key_events 为空 → what 回落章标题。"""
    bp = Blueprint.blank()
    bp.upsert("chapters", _gist(1, 2, title="闭关九十日", after_days=90))
    pend = synthesize_worldstate(bp)["pending"]
    assert len(pend) == 1 and pend[0]["what"] == "闭关九十日"


def test_synthesize_no_after_days_empty_pending():
    """无 after_days（ingest 编码的章）→ pending 为空，行为兼容。"""
    bp = Blueprint.blank()
    bp.upsert("chapters", _gist(1, 1, events=["旧稿事件"], done=True))
    bp.upsert("chapters", _gist(1, 2, events=["旧稿事件二"], done=True))
    ws = synthesize_worldstate(bp)
    assert ws["pending"] == []


def test_synthesize_pending_out_of_order_chapters():
    """蓝图 chapters 乱序 → 按 (vol, ch) 排序后累计，due 不受落盘顺序影响。"""
    bp = Blueprint.blank()
    bp.upsert("chapters", _gist(1, 3, events=["丙"], after_days=3))
    bp.upsert("chapters", _gist(1, 1, events=["甲"], after_days=0))
    bp.upsert("chapters", _gist(1, 2, events=["乙"], after_days=90))
    pend = synthesize_worldstate(bp)["pending"]
    assert [(p["id"], p["due"]) for p in pend] == [("pd:ke-1-2", 90), ("pd:ke-1-3", 93)]


def test_synthesize_pending_matches_schema():
    """登记条目通过 worldstate.schema.json 校验（id pattern / status 枚举）。"""
    schema = json.loads((_SCHEMAS / "bible" / "worldstate.schema.json").read_text("utf-8"))
    bp = Blueprint.blank()
    bp.upsert("chapters", _gist(1, 2, events=["突破筑基"], after_days=30))
    bp.upsert("chapters", _gist(1, 5, events=["秘境开启"], after_days=365))
    ws = synthesize_worldstate(bp)
    jsonschema.validate(ws, schema)  # 不抛即通过


# ---------------------------------------------------- ingest 约定条目对齐


def test_parse_pending_line_scheduled_alignment():
    """ingest 抽取行 → parse_pending_line → 字段与 add_pending 对齐
    （status 必须 scheduled；due/span 语义与「登记时 now=0」一致）。"""
    what, dt, warn = parse_pending_line("约定：三日后闭关｜+3日")
    assert warn is None and dt == 3
    item = {"id": "pd:ingest1", "who": "", "what": what, "due": dt,
            "span": max(dt, 1), "created_t": 0, "status": "scheduled",
            "created_at": {"vol": 1, "ch": 0}, "overdue": 0, "block_count": 0}
    schema = json.loads((_SCHEMAS / "bible" / "worldstate.schema.json").read_text("utf-8"))
    jsonschema.validate({"characters": {}, "pending": [item]}, schema)
