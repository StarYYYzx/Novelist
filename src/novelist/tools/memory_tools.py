"""记忆检索工具（docs/05 §4.1 / docs/07 §7）：query_memory / get_character_history / get_plot_events / reindex_memory。

检索走 `core.memory.MemoryRetriever`（docs/07 §7.1）：
- 有真实 Embedding Provider 时走语义检索；
- 无则退化为 `KeywordEmbedding` 关键词索引（F9.4 降级路径，接口一致、结果可用）。

索引（`memory/fragment_index.json` + `memory/rag/vectors.json`）是**可再生缓存**，
缺失时自动从 `memory/` 事实源重建（ADR-016：文件才是事实源）。
"""

from __future__ import annotations

import json

from ..core.memory import MemoryIndex, MemoryQuery, MemoryRetriever, reindex_memory
from ..core.session import SessionInfo
from ..core.tools import Tool, ok
from ..storage.workspace import Workspace

# AG-11（2026-09-15 审计）：检索类观测一律有界——单条片段/单次返回都有上限，
# 否则 `get_plot_events` 会随剧情推进线性膨胀，几轮取证就顶穿上下文预算。
_HIT_TEXT_CHARS = 600
_EVENTS_DEFAULT_LIMIT = 200
_RECENT_DEFAULT_LIMIT = 20


def _load_index(ws: Workspace, project_id: str, embedding=None) -> MemoryIndex:
    """装载索引；缺失或为空时从文件事实源重建。"""
    p = ws.fragment_index_path(project_id)
    idx = MemoryIndex.load(ws, project_id) if p.exists() else None
    if idx is None or not idx.fragments:
        idx = MemoryIndex()
        idx.rebuild(ws, project_id, embedding)
    return idx


def tools(ws: Workspace, embedding=None) -> list[Tool]:
    def _query_memory(session: SessionInfo, params, budget=None):
        q = str(params.get("query", "") or "")
        top_k = int(params.get("top_k", 5) or 5)
        filters = params.get("filters") or {}
        # 便捷过滤：char_id / kinds 也可从顶层传入（LLM 更容易填对）
        if "char_id" in params and "char_id" not in filters:
            filters["char_id"] = params["char_id"]
        if "kinds" in params and "kinds" not in filters:
            filters["kinds"] = params["kinds"]
        min_score = float(params.get("min_score", 0.0) or 0.0)

        idx = _load_index(ws, session.project_id, embedding)
        retriever = MemoryRetriever(idx, embedding=embedding, min_score=min_score)
        hits = retriever.query(MemoryQuery(query=q, filters=filters, top_k=top_k, min_score=min_score))
        return ok(
            data={
                "hits": [
                    {
                        "sig": h.sig,
                        "kind": h.kind,
                        "text": (h.text or "")[:_HIT_TEXT_CHARS],
                        "source": h.source,
                        "refs": h.refs,
                        "score": h.score,
                    }
                    for h in hits
                ],
                "total": len(idx.fragments),
                "mode": getattr(embedding, "kind", "keyword-hash"),
            }
        )

    def _history(session: SessionInfo, params, budget=None):
        char_id = str(params.get("char_id", "") or "")
        p = ws.char_history_path(session.project_id, char_id)
        text = ""
        entries: list = []
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                entries = list(data.get("entries") or []) if isinstance(data, dict) else []
                text = json.dumps(data, ensure_ascii=False)
            except (ValueError, OSError):  # pragma: no cover
                text, entries = "", []
        limit = int(params.get("limit", 0) or 0)
        recent = entries[-limit:] if limit > 0 else entries[-_RECENT_DEFAULT_LIMIT:]
        return ok(data={"char_id": char_id, "history": text[:500], "count": len(entries),
                        "recent": recent, "recent_limited": limit <= 0})

    def _plot_events(session: SessionInfo, params, budget=None):
        """读取剧情事件流（事实源 memory/plot_events.json），可按卷/章过滤。"""
        path = ws._abs(f"{session.project_id}/memory/plot_events.json")
        events = []
        if path.exists():
            try:
                events = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(events, list):
                    events = []
            except (ValueError, OSError):  # pragma: no cover
                events = []
        vol = params.get("vol")
        ch = params.get("ch")
        if vol is not None:
            events = [e for e in events if (e.get("at") or {}).get("vol", e.get("vol")) == int(vol)]
        if ch is not None:
            events = [e for e in events if (e.get("at") or {}).get("ch", e.get("ch")) == int(ch)]
        limit = int(params.get("limit", 0) or 0)
        total = len(events)
        # AG-11：默认给上限（此前不传 limit 即返回全部事件，越写越长）
        take = limit if limit > 0 else _EVENTS_DEFAULT_LIMIT
        events = events[-take:]
        return ok(data={"events": events, "count": len(events), "total": total,
                        "truncated": total > len(events)})

    def _reindex(session: SessionInfo, params, budget=None):
        """全量重建记忆索引（docs/07 §7.3）。sensitive——会重写索引文件。"""
        n = reindex_memory(ws, session.project_id, embedding=embedding)
        return ok(data={"fragments": n, "mode": getattr(embedding, "kind", "keyword-hash")})

    return [
        Tool(
            "query_memory",
            "检索相关历史经历/剧情/关系（语义检索，无 embedding 时降级为关键词）",
            "safe",
            _query_memory,
            {
                "query": {"type": "string"},
                "top_k": {"type": "integer"},
                "char_id": {"type": "string"},
                "kinds": {"type": "array", "items": {"type": "string"}},
                "min_score": {"type": "number"},
                "filters": {"type": "object"},
            },
            required=["query"],
        ),
        Tool(
            "get_character_history",
            "获取某角色经历摘要",
            "safe",
            _history,
            {"char_id": {"type": "string"}, "limit": {"type": "integer"}},
            required=["char_id"],
        ),
        Tool(
            "get_plot_events",
            "读取剧情事件流（可按卷/章过滤；默认只回最近 200 条）",
            "safe",
            _plot_events,
            {"vol": {"type": "integer"}, "ch": {"type": "integer"}, "limit": {"type": "integer"}},
        ),
        Tool(
            "reindex_memory",
            "从 memory/ 事实源全量重建记忆检索索引",
            "sensitive",
            _reindex,
            {},
        ),
    ]
