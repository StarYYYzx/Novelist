"""记忆检索工具（docs/05 §4.1 / docs/07 §7.1）：query_memory / get_character_history（safe）。

M1：提供可用的**关键词降级**检索（docs/07 §7.3 —— 无 Embedding 时退化为中文分词/子串匹配，
接口与未来语义检索一致）。命中返回摘要 + 来源定位 + refs。
"""

from __future__ import annotations

from ..core.session import SessionInfo
from ..core.tools import Tool, ok
from ..storage.workspace import Workspace


def _harvest_texts(ws: Workspace, project_id: str) -> list[dict]:
    """扫描 memory/ 与 bible/ 下的 JSON 文件，抽出可检索文本片段。"""
    items: list[dict] = []
    mem_dir = ws.memory_dir(project_id)
    if mem_dir.is_dir():
        for f in sorted(mem_dir.rglob("*.json")):
            try:
                import json

                data = json.loads(f.read_text(encoding="utf-8"))
                items.append({"source": str(f.name), "text": _flatten(data)})
            except (ValueError, OSError):  # pragma: no cover - 跳过坏文件
                continue
    return items


def _flatten(data) -> str:
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        return " ".join(_flatten(x) for x in data)
    if isinstance(data, dict):
        return " ".join(_flatten(v) for k, v in data.items())
    return str(data)


def tools(ws: Workspace) -> list[Tool]:
    def _query_memory(session: SessionInfo, params, budget=None):
        q = params.get("query", "")
        top_k = int(params.get("top_k", 5))
        hits = []
        for item in _harvest_texts(ws, session.project_id):
            if not q or q.lower() in item["text"].lower():
                hits.append(
                    {"sig": f"{item['source']}:{item['text'][:20]}", "kind": "keyword",
                     "text": item["text"][:200], "source": {"vol": 0, "ch": 0}, "refs": [], "score": 1.0}
                )
                if len(hits) >= top_k:
                    break
        return ok(data={"hits": hits})

    def _history(session: SessionInfo, params, budget=None):
        char_id = params.get("char_id", "")
        p = ws.char_history_path(session.project_id, char_id)
        text = ""
        if p.exists():
            try:
                import json

                text = json.dumps(json.loads(p.read_text(encoding="utf-8")), ensure_ascii=False)
            except (ValueError, OSError):  # pragma: no cover
                text = ""
        return ok(data={"char_id": char_id, "history": text[:500]})

    return [
        Tool("query_memory", "检索相关历史经历/剧情（关键词降级）", "safe", _query_memory,
             {"query": {"type": "string"}, "top_k": {"type": "integer"}}),
        Tool("get_character_history", "获取某角色经历摘要", "safe", _history,
             {"char_id": {"type": "string"}}),
    ]
