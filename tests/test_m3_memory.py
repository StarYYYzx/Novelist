"""M3 记忆子系统测试（docs/04 §5.8 / docs/06 §3.5 / docs/07 §7，ADR-011，F9）。

覆盖：
- Embedding：关键词降级的确定性与归一化、余弦排序、无 key 时自动降级。
- 索引：从 memory/ 事实源收割与重建、rag/ 被删后仍可检索（可再生）。
- 检索：相关性排序、char_id / kinds / 章节范围过滤。
- 写入与冲突双检：重复入库、bible 引用完整性、语义层注入 → 冲突即回退不入库。
- 事件回写与索引增量：落定即索引，下一书写点立即可"先忆"（A9）。
- 工具：query_memory / get_character_history / get_plot_events / reindex_memory 门禁。
"""

from __future__ import annotations

import json

import pytest

from novelist.core.embedding import KeywordEmbedding, cosine, make_embedding, tokenize
from novelist.core.memory import (
    MemoryConflictError,
    MemoryIndex,
    MemoryQuery,
    MemoryRetriever,
    MemoryWriter,
    harvest_fragments,
    reindex_memory,
)
from novelist.core.session import SessionInfo
from novelist.core.writeback import ContradictionError, LandedEvent, commit_event
from novelist.storage.checkpoint import Checkpoint
from novelist.storage.workspace import Workspace
from novelist.tools import build_registry


def _project(tmp_path, pid="proj-mem"):
    ws = Workspace(root=str(tmp_path))
    ws.create_project(pid)
    Checkpoint(ws).save(pid, {"id": pid, "title": "t", "pipeline_state": "正文", "event_seq": 0})
    return ws, pid


def _write(ws, pid, rel, data):
    ws.write_json(ws._abs(f"{pid}/{rel}"), data)


def _seed_memory(ws, pid):
    """预置一段"历史"：1 个剧情事件 + 2 条人物经历 + 1 次关系变化。"""
    _write(ws, pid, "bible/characters.json", [{"id": "char:cz7", "name": "苏晚"}, {"id": "char:bds", "name": "大师兄"}])
    _write(ws, pid, "memory/plot_events.json", [
        {"id": "ev:1", "at": {"vol": 1, "ch": 1}, "type": "discovery",
         "summary": "苏晚在青云试炼中发现袖中断玉佩", "participants": ["char:cz7"], "affected_threads": []},
    ])
    _write(ws, pid, "memory/character_histories/char_cz7.json", {
        "char_id": "char:cz7", "revision": 1,
        "entries": [{"at": {"vol": 1, "ch": 1}, "summary": "苏晚初入青云宗，被掌门收为弟子"}],
    })
    _write(ws, pid, "memory/character_histories/char_bds.json", {
        "char_id": "char:bds", "revision": 1,
        "entries": [{"at": {"vol": 1, "ch": 2}, "summary": "大师兄在演武场击败同门，声望大增"}],
    })
    _write(ws, pid, "memory/relationships.json", {
        "pairs": [{"a": "char:cz7", "b": "char:bds",
                   "entries": [{"at": {"vol": 1, "ch": 2}, "from": "陌生", "to": "敌对"}]}],
    })


# ---------------------------------------------------------------- Embedding


def test_keyword_embedding_is_deterministic_and_normalized():
    emb = KeywordEmbedding(dim=64)
    a = emb.embed(["苏晚发现断玉佩"])[0]
    b = emb.embed(["苏晚发现断玉佩"])[0]
    assert a == b, "关键词向量必须跨调用确定性（不得用带随机盐的内置 hash）"
    assert len(a) == 64
    norm = sum(x * x for x in a) ** 0.5
    assert abs(norm - 1.0) < 1e-6, "向量应 L2 归一化，避免长文本天然高分"


def test_tokenize_splits_cjk_into_bigrams():
    toks = tokenize("苏晚")
    assert "苏晚" in toks
    assert tokenize("Ada Lovelace")[0] == "ada"


def test_cosine_returns_zero_on_dim_mismatch():
    assert cosine([1.0, 0.0], [1.0]) == 0.0
    assert cosine([], []) == 0.0


def test_make_embedding_degrades_to_keyword_without_key(monkeypatch):
    """F9.4：无 key / 缺依赖时自动降级为关键词索引，不抛错。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    emb = make_embedding("openai")
    assert isinstance(emb, KeywordEmbedding)
    assert make_embedding(None).kind == "keyword-hash"
    assert make_embedding("keyword-fallback").kind == "keyword-hash"


# ---------------------------------------------------------------- 索引与检索


def test_harvest_and_reindex_collects_all_kinds(tmp_path):
    ws, pid = _project(tmp_path)
    _seed_memory(ws, pid)
    frs = harvest_fragments(ws, pid)
    kinds = {f.kind for f in frs}
    assert kinds == {"plot_event", "experience", "relationship"}, kinds
    assert len(frs) == 4  # 1 事件 + 2 经历 + 1 关系

    n = reindex_memory(ws, pid)
    assert n == 4
    idx = MemoryIndex.load(ws, pid)
    assert len(idx.fragments) == 4
    assert idx.revision == 1
    # 关键词模式不落向量：打分走精确 token，向量只是无谓开销
    assert idx.kind == "keyword-hash"
    assert not (ws.rag_dir(pid) / "vectors.json").exists()


class _FakeSemanticEmbedding:
    """测试用语义 embedding：kind 非 keyword → 检索走向量余弦路径。"""

    kind = "fake-semantic"
    dim = 4

    def embed(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] if "断玉佩" in t else [0.0, 1.0, 0.0, 0.0] for t in texts]


def test_semantic_path_persists_vectors_and_scores_by_cosine(tmp_path):
    """有真实 Embedding 时走语义路径：向量落盘 + 余弦打分（docs/07 §7.1）。"""
    ws, pid = _project(tmp_path)
    _seed_memory(ws, pid)
    idx = MemoryIndex()
    idx.rebuild(ws, pid, _FakeSemanticEmbedding())
    assert idx.kind == "fake-semantic"
    assert (ws.rag_dir(pid) / "vectors.json").exists(), "语义模式应落向量缓存"

    hits = MemoryRetriever(idx, embedding=_FakeSemanticEmbedding()).query(
        MemoryQuery(query="断玉佩", top_k=3)
    )
    assert hits[0].score == 1.0
    assert "断玉佩" in hits[0].text
    assert all(h.score == 0.0 for h in hits[1:])


def test_retrieval_ranks_relevant_fragment_first(tmp_path):
    ws, pid = _project(tmp_path)
    _seed_memory(ws, pid)
    idx = MemoryIndex()
    idx.rebuild(ws, pid)
    hits = MemoryRetriever(idx).query(MemoryQuery(query="苏晚的断玉佩", top_k=3))
    assert hits, "关键词降级路径也必须能召回"
    assert "断玉佩" in hits[0].text
    assert hits[0].kind == "plot_event"
    # 命中带来源定位与 bible 引用（docs/07 §7.1）
    assert hits[0].source["vol"] == 1 and hits[0].source["ch"] == 1
    assert "char:cz7" in hits[0].refs
    assert hits[0].score > 0


def test_retrieval_filters_by_char_id_and_kind(tmp_path):
    ws, pid = _project(tmp_path)
    _seed_memory(ws, pid)
    idx = MemoryIndex()
    idx.rebuild(ws, pid)

    cz7 = MemoryRetriever(idx).query(
        MemoryQuery(query="苏晚", filters={"char_id": "char:cz7"}, top_k=10)
    )
    assert cz7 and all("char:cz7" in h.refs or h.text for h in cz7)
    assert all("大师兄" not in h.text for h in cz7), "演员隔离：查 A 不应召回 B 的专属经历"

    only_rel = MemoryRetriever(idx).query(MemoryQuery(query="关系", filters={"kinds": ["relationship"]}, top_k=10))
    assert only_rel and all(h.kind == "relationship" for h in only_rel)


def test_retrieval_filters_by_chapter_scope(tmp_path):
    ws, pid = _project(tmp_path)
    _seed_memory(ws, pid)
    idx = MemoryIndex()
    idx.rebuild(ws, pid)
    hits = MemoryRetriever(idx).query(MemoryQuery(query="苏晚", filters={"chapter_scope": {"vol": 1, "ch": 2}}, top_k=10))
    assert all(h.source["vol"] == 1 and h.source["ch"] == 2 for h in hits)


def test_query_works_without_any_vector_cache(tmp_path):
    """检索不依赖 rag/ 缓存：无向量（关键词模式 / 缓存被删）仍能工作（ADR-016 可再生）。"""
    ws, pid = _project(tmp_path)
    _seed_memory(ws, pid)
    idx = MemoryIndex()
    idx.rebuild(ws, pid, _FakeSemanticEmbedding())  # 先造出语义模式的向量缓存
    assert idx.vectors
    (ws.rag_dir(pid) / "vectors.json").unlink()     # 再删掉

    idx2 = MemoryIndex.load(ws, pid)
    assert idx2.vectors == {}
    hits = MemoryRetriever(idx2).query(MemoryQuery(query="断玉佩", top_k=1))
    assert hits and "断玉佩" in hits[0].text


# ---------------------------------------------------------------- 写入与冲突双检


def test_writer_appends_and_indexes_incrementally(tmp_path):
    ws, pid = _project(tmp_path)
    _seed_memory(ws, pid)
    reindex_memory(ws, pid)
    before = len(MemoryIndex.load(ws, pid).fragments)

    w = MemoryWriter(ws, pid)
    w.append_plot_event({
        "id": "ev:2", "at": {"vol": 1, "ch": 3}, "type": "conflict",
        "summary": "苏晚与大师兄在藏经阁对峙", "participants": ["char:cz7", "char:bds"], "affected_threads": [],
    })
    idx = MemoryIndex.load(ws, pid)
    assert len(idx.fragments) == before + 1, "写入应增量入索引，下一事件立即可检索（F11.4）"
    hits = MemoryRetriever(idx).query(MemoryQuery(query="藏经阁对峙", top_k=1))
    assert hits and "藏经阁" in hits[0].text


def test_writer_records_relationship_change(tmp_path):
    ws, pid = _project(tmp_path)
    _seed_memory(ws, pid)
    w = MemoryWriter(ws, pid)
    w.record_relationship_change("char:cz7", "char:bds",
                                 {"at": {"vol": 2, "ch": 1}, "from": "敌对", "to": "亦敌亦友"})
    data = json.loads(ws.relationships_path(pid).read_text(encoding="utf-8"))
    pair = data["pairs"][0]
    assert pair["entries"][-1]["to"] == "亦敌亦友"
    hits = MemoryRetriever(MemoryIndex.load(ws, pid)).query(MemoryQuery(query="亦敌亦友", top_k=3))
    assert any(h.kind == "relationship" for h in hits)


def test_duplicate_writeback_is_rejected(tmp_path):
    """重复入库即冲突：回退并提请人工仲裁，不静默污染记忆（docs/06 §4.4）。"""
    ws, pid = _project(tmp_path)
    _write(ws, pid, "bible/characters.json", [{"id": "char:cz7", "name": "苏晚"}])
    ev = LandedEvent(project_id=pid, vol=1, ch=1, seq=1, kind="conflict", summary="苏晚击败强敌")
    assert commit_event(ev, SessionInfo(project_id=pid, agent="orchestrator"), ws=ws)
    with pytest.raises(ContradictionError, match="重复"):
        commit_event(ev, SessionInfo(project_id=pid, agent="orchestrator"), ws=ws)
    # 未被写入第二次
    events = json.loads(ws._abs(f"{pid}/memory/plot_events.json").read_text(encoding="utf-8"))
    assert len(events) == 1


def test_bible_ref_integrity_conflict(tmp_path):
    """引用未建档的 bible 实体 → 冲突（docs/06 §5.2）。"""
    ws, pid = _project(tmp_path)
    _write(ws, pid, "bible/plot_threads.json", [{"id": "pt:V001", "desc": "已知伏笔"}])
    w = MemoryWriter(ws, pid)
    with pytest.raises(MemoryConflictError, match="未建档"):
        w.append_plot_event({
            "id": "ev:x", "at": {"vol": 1, "ch": 1}, "type": "reveal",
            "summary": "断玉佩的秘密揭晓", "participants": [], "affected_threads": ["pt:V999"],
        })


def test_semantic_layer_conflict_blocks_write(tmp_path):
    """语义层双检注入后，判冲突即回退、不落盘（docs/05 §5.4 第 4 步）。"""
    ws, pid = _project(tmp_path)
    _write(ws, pid, "bible/characters.json", [{"id": "char:cz7", "name": "苏晚"}])
    ev = LandedEvent(project_id=pid, vol=1, ch=1, seq=1, kind="conflict", summary="苏晚战死")
    # 与既有记忆"苏晚还活着"矛盾 → 语义层判 False
    with pytest.raises(ContradictionError, match="语义冲突"):
        commit_event(ev, SessionInfo(project_id=pid, agent="orchestrator"), ws=ws,
                     semantic_checker=lambda new, existing: False)
    assert not ws._abs(f"{pid}/memory/plot_events.json").exists(), "冲突不得入库"


def test_without_semantic_checker_only_rule_layer_runs(tmp_path):
    """未注入语义层时仅做规则层双检，不应阻断（降级可用）。"""
    ws, pid = _project(tmp_path)
    _write(ws, pid, "bible/characters.json", [{"id": "char:cz7", "name": "苏晚"}])
    ev = LandedEvent(project_id=pid, vol=1, ch=1, seq=1, kind="conflict", summary="苏晚继任掌门",
                     participants=["char:cz7"])
    assert commit_event(ev, SessionInfo(project_id=pid, agent="orchestrator"), ws=ws)


# ---------------------------------------------------------------- 工具


def _invoke(ws, pid, tool, params, **kw):
    reg = build_registry(ws, **kw)
    sess = SessionInfo(project_id=pid, agent="test")
    return reg.invoke(sess, tool, params)


def test_query_memory_tool_returns_hits(tmp_path):
    ws, pid = _project(tmp_path)
    _seed_memory(ws, pid)
    res = _invoke(ws, pid, "query_memory", {"query": "断玉佩", "top_k": 3})
    assert res.status == "ok", res.data
    assert res.data["hits"], "先忆必须能召回（F3.4）"
    assert res.data["hits"][0]["source"]["vol"] == 1
    assert res.data["hits"][0]["source"]["ch"] == 1
    assert res.data["mode"] == "keyword-hash"


def test_get_character_history_tool(tmp_path):
    ws, pid = _project(tmp_path)
    _seed_memory(ws, pid)
    res = _invoke(ws, pid, "get_character_history", {"char_id": "char:cz7"})
    assert res.status == "ok"
    assert res.data["count"] == 1
    assert "青云宗" in res.data["history"]


def test_get_plot_events_tool_filters(tmp_path):
    ws, pid = _project(tmp_path)
    _seed_memory(ws, pid)
    res = _invoke(ws, pid, "get_plot_events", {"vol": 1})
    assert res.status == "ok"
    assert res.data["count"] == 1
    assert _invoke(ws, pid, "get_plot_events", {"vol": 9}).data["count"] == 0


def test_reindex_memory_tool_is_gated(tmp_path):
    """reindex_memory 为 sensitive：默认走 ask，无审批通道时拒绝（F6.1）。"""
    from novelist.core.errors import DeniedError
    from novelist.core.tools import PermissionGate

    ws, pid = _project(tmp_path)
    _seed_memory(ws, pid)
    with pytest.raises(DeniedError):
        _invoke(ws, pid, "reindex_memory", {})
    gate = PermissionGate(profiles={"supervised": {"sensitive": "allow", "danger": "deny", "tools": {}}})
    res = _invoke(ws, pid, "reindex_memory", {}, gate=gate)
    assert res.status == "ok"
    assert res.data["fragments"] == 4


# ---------------------------------------------------------------- 端到端闭环


def test_writeback_then_recall_closed_loop(tmp_path):
    """A9 闭环：事件落定 → 增量入索引 → 下一章"先忆"能召回（不等章末）。"""
    from click.testing import CliRunner

    from novelist.cli import _compose_goal, cli

    ws, pid = _project(tmp_path)
    _write(ws, pid, "bible/characters.json", [{"id": "char:cz7", "name": "苏晚"}])
    res = CliRunner().invoke(cli, ["chapter", str(tmp_path), "--provider", "scripted", "--vol", "1", "--ch", "1"])
    assert res.exit_code == 0, res.output

    assert ws.fragment_index_path(pid).exists(), "回写应顺带落索引"
    idx = MemoryIndex.load(ws, pid)
    assert any("完成第 1 卷第 1 章" in f.text for f in idx.fragments)

    goal2 = _compose_goal(ws, pid, 1, 2)
    assert "完成第 1 卷第 1 章" in goal2, "下一章先忆应召回上一章已落定的事件（A9/NFR-13）"


def test_recall_ranks_by_relevance_not_recency(tmp_path):
    """检索按相关度排序而非"最近 N 条"，且只注入摘要不拖全文（docs/04 §5.8）。"""
    from novelist.cli import _compose_goal

    ws, pid = _project(tmp_path)
    _write(ws, pid, "bible/characters.json", [{"id": "char:cz7", "name": "苏晚"}])
    _write(ws, pid, "memory/plot_events.json", [
        {"id": "ev:1", "at": {"vol": 1, "ch": 1}, "summary": "苏晚在青云试炼中发现袖中断玉佩", "participants": ["char:cz7"]},
        {"id": "ev:2", "at": {"vol": 1, "ch": 2}, "summary": "大师兄整顿藏经阁典籍", "participants": []},
        {"id": "ev:3", "at": {"vol": 1, "ch": 3}, "summary": "宗门长老议事，粮草调度", "participants": []},
    ])
    gist = ws.outline_chapter_path(pid, 1, 4)
    gist.parent.mkdir(parents=True, exist_ok=True)
    gist.write_text("细纲：断玉佩的秘密在掌门继任大典上被揭开。", encoding="utf-8")

    goal = _compose_goal(ws, pid, 1, 4)
    hits = [ln for ln in goal.splitlines() if ln.startswith("- [")]
    assert hits, "先忆必须有命中（F3.4）"
    assert "断玉佩" in hits[0], f"最相关（但最旧）的记忆应排第一，实际首位：{hits[0]}"
