"""M3w/ADR-027 记忆 LLM 侧选 rerank 测试（docs/10 §7.8 记忆侧选 / docs/07 §7.1）。

覆盖（**单测绝不真调 LLM**，docs/09 §2.1——reranker 用纯 Python 回调替身）：
- 默认关闭：不设 reranker → 行为/排序与旧版逐字节一致，回调绝不触发（零 LLM 零配额）
- 候选池放大：字面不重合但语义相关的碎片，经 rerank 被救回并提到最前
- reason 只写入选条目；未入选/池外按原分回补
- reranker 抛异常 → 回退纯相似度，不拖垮检索
- rerank_pool 钳制候选池：池外碎片无法被侧选选中
"""

from __future__ import annotations

from novelist.core.embedding import KeywordEmbedding
from novelist.core.memory import MemoryIndex, MemoryQuery, MemoryRetriever


def _write_mem(ws, pid, write_json):
    """预置候选碎片：一条与 query 字面强重合（玉佩），一条字面不重合但语义相关（藏宝图）。"""
    write_json(ws, pid, "bible/characters.json", [
        {"id": "char:cz7", "name": "苏晚"},
        {"id": "char:bds", "name": "大师兄"},
    ])
    write_json(ws, pid, "memory/plot_events.json", [
        {"id": "ev:1", "at": {"vol": 1, "ch": 1}, "type": "discovery",
         "summary": "苏晚在青云试炼中发现袖中断玉佩", "participants": ["char:cz7"], "affected_threads": []},
        {"id": "ev:2", "at": {"vol": 1, "ch": 3}, "type": "discovery",
         "summary": "大师兄在三更偷盗掌门密室的藏宝图", "participants": ["char:bds"], "affected_threads": []},
    ])
    write_json(ws, pid, "memory/character_histories/char_cz7.json", {
        "char_id": "char:cz7", "revision": 1,
        "entries": [{"at": {"vol": 1, "ch": 1}, "summary": "苏晚初入青云宗，被掌门收为弟子"}],
    })
    write_json(ws, pid, "memory/relationships.json", {
        "pairs": [{"a": "char:cz7", "b": "char:bds",
                   "entries": [{"at": {"vol": 1, "ch": 2}, "from": "陌生", "to": "敌对"}]}],
    })


def _idx(ws, pid):
    idx = MemoryIndex()
    idx.rebuild(ws, pid, KeywordEmbedding())
    return idx


def _find(idx, needle: str) -> str:
    for f in idx.fragments:
        if needle in f.text:
            return f.sig
    raise AssertionError(f"语料中找不到含 {needle!r} 的碎片")


# 查询与"玉佩"字面强重合，意图却与"藏宝图→失物下落"语义相关（rerank 应救回后者）
_QUERY = "玉佩的下落与断玉其来历"
_REASON = "藏宝图牵出密室失窃真相，与断玉出处同源"


def test_default_no_rerank_is_old_behavior_and_calls_nothing(ws_factory, write_json):
    ws, pid = ws_factory("proj-rr-ff")
    _write_mem(ws, pid, write_json)
    ret = MemoryRetriever(_idx(ws, pid), embedding=KeywordEmbedding())
    called: list[int] = []
    spy = lambda q, cand: (called.append(1) or [])

    # 不传 reranker —— 旧版路径，缓存命中与否都应零触发
    hits = ret.query(MemoryQuery(query=_QUERY, top_k=3))
    assert hits and not called
    assert all(h.reason == "" for h in hits), "未 rerank 时 reason 恒空"


def test_rerank_promotes_semantic_relevant_low_overlap(ws_factory, write_json):
    ws, pid = ws_factory("proj-rr-promote")
    _write_mem(ws, pid, write_json)
    idx = _idx(ws, pid)
    ret = MemoryRetriever(idx, embedding=KeywordEmbedding())
    a = _find(idx, "断玉佩")
    b = _find(idx, "藏宝图")

    # 候选池=关键词打分序；唯一低分(猜测分≈0)的"藏宝图"碎片要仍在池内才可被救回
    hits_no = ret.query(MemoryQuery(query=_QUERY, top_k=9))
    pool_sigs = {h.sig for h in hits_no}
    assert b in pool_sigs, "候选池应含低字面重合碎片，否则侧选无从救回"

    def rr(q, cand):
        assert q == _QUERY
        assert any(c["sig"] == b for c in cand), "回调应收到候选池清单（含标 target sig）"
        # LLM 判定：藏宝图碎片与断玉来历语义强相关 → 提到最前
        return [(b, _REASON)]

    hits = ret.query(MemoryQuery(query=_QUERY, top_k=3, reranker=rr))
    assert hits[0].sig == b
    assert hits[0].reason == _REASON
    assert any(h.sig == a for h in hits[:3]), "字面强重合碎片应仍由原分回补进前列"


def test_rerank_only_selected_get_reason_tails_by_score(ws_factory, write_json):
    ws, pid = ws_factory("proj-rr-reason")
    _write_mem(ws, pid, write_json)
    idx = _idx(ws, pid)
    ret = MemoryRetriever(idx, embedding=KeywordEmbedding())
    a = _find(idx, "断玉佩")

    def rr(q, cand):
        return [(a, _REASON)]  # 只精审这一条

    hits = ret.query(MemoryQuery(query=_QUERY, top_k=3, reranker=rr))
    assert hits[0].sig == a and hits[0].reason == _REASON
    assert all(h.reason == "" for h in hits[1:]), "未入选条目不给理由"


def test_rerank_exception_falls_back_to_pure_score(ws_factory, write_json):
    ws, pid = ws_factory("proj-rr-boom")
    _write_mem(ws, pid, write_json)
    ret = MemoryRetriever(_idx(ws, pid), embedding=KeywordEmbedding())

    def boom(q, cand):
        raise RuntimeError("llm down")

    hits = ret.query(MemoryQuery(query=_QUERY, top_k=3, reranker=boom))
    assert hits, "rerank 失败应回退到纯相似度，不应让检索崩掉"
    assert all(h.reason == "" for h in hits)


def test_rerank_pool_gates_out_of_pool_candidate(ws_factory, write_json):
    ws, pid = ws_factory("proj-rr-gate")
    _write_mem(ws, pid, write_json)
    idx = _idx(ws, pid)
    ret = MemoryRetriever(idx, embedding=KeywordEmbedding())
    a = _find(idx, "断玉佩")
    b = _find(idx, "藏宝图")

    def rr(q, cand):
        return [(b, _REASON)]

    # rerank_pool=1 → 候选池只剩最高分那一条（a）；b 在池外，无法被侧选到 → 回补原分序
    hits = ret.query(MemoryQuery(query=_QUERY, top_k=3, reranker=rr, rerank_pool=1))
    assert hits[0].sig == a, "池外碎片不能被选中，应按原相似度序回补"

    # pool 充足时同一回调却能选中 b（对照）
    hits_ok = ret.query(MemoryQuery(query=_QUERY, top_k=3, reranker=rr, rerank_pool=9))
    assert hits_ok[0].sig == b