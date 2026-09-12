"""D4 回归：章节细纲 threads_involved 引用不存在的线程 id。

背景（2026-09-03 新书 5 章实测）：thread_set 产出 pt:1..pt:7，chapter 节点却自造
语义名 `pt:jade_talisman`（prompt 只说"用 pt:xxx"没给合法清单）→ V2 block。
修复 = ①prompt 注入本书伏笔清单（id+desc）并禁止自造；②apply 层对齐 characters
的悬空引用处理——丢弃 + warning。
"""

from __future__ import annotations

from novelist.forge.nodes import NodeContext, _APPLY, _PROMPTS
from novelist.forge.state import Blueprint
from novelist.providers.fake import FakeProvider

SEED_META = {"title": "t", "genre": "修仙", "logline": "x",
             "scale": {"volumes": 1, "chapters_per_volume": 2, "target_words_per_chapter": 100}}


def _mk_ctx(ws_factory, pid: str, *, vol: int = 1, ch: int = 1) -> tuple:
    ws, proj = ws_factory(pid)
    bp = Blueprint.blank(dict(SEED_META))
    bp.upsert("threads", {"id": "pt:1", "desc": "玉符来历"})
    bp.upsert("threads", {"id": "pt:2", "desc": " rival 线"})
    ctx = NodeContext(ws=ws, project_id=proj, bp=bp, provider=FakeProvider(reply="x"),
                      pack={}, vol=vol, ch=ch)
    return ws, proj, bp, ctx


def test_chapter_prompt_lists_valid_thread_ids(ws_factory):
    """prompt 必须注入本书伏笔清单——模型只能从这里选，无从自造语义名。"""
    _, _, _, ctx = _mk_ctx(ws_factory, "proj-d4a")
    _, user = _PROMPTS["chapter"](ctx)
    assert "pt:1" in user and "pt:2" in user
    assert "禁止自造" in user


def test_apply_drops_dangling_thread_ref(ws_factory):
    """apply 兜底：合法 id 保留、悬空 id 丢弃并告警（对齐 characters 悬空处理）。"""
    ws, proj, bp, ctx = _mk_ctx(ws_factory, "proj-d4b")
    warns = _APPLY["chapter"](ctx, {"artifact": {
        "title": "测试章", "key_events": ["事件一"],
        "characters": [], "threads_involved": ["pt:1", "pt:jade_talisman"],
        "after_days": 0}})
    gist_path = ws.outline_chapter_path(proj, 1, 1)
    text = gist_path.read_text(encoding="utf-8")
    assert "pt:1" in text
    assert "pt:jade_talisman" not in text
    assert any("pt:jade_talisman" in w for w in warns)


def test_apply_keeps_empty_when_no_threads_touched(ws_factory):
    ws, proj, _, ctx = _mk_ctx(ws_factory, "proj-d4c")
    _APPLY["chapter"](ctx, {"artifact": {
        "title": "安静章", "key_events": ["日常"], "characters": [],
        "threads_involved": [], "after_days": 0}})
    assert "伏笔" not in ws.outline_chapter_path(proj, 1, 1).read_text(encoding="utf-8")
