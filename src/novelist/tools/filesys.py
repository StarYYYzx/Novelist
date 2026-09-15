"""文件系统工具（docs/05 §4.1）：read_file / write_file / grep_text（safe）。

所有读写经 Workspace 沙箱根解析，拒绝越界（docs/06 §7）。Actor 只能写自己的 take 由
权限面/路径约束在调用层保证（此处工具是通用 safe，越权由 SessionInfo + 上层控制）。

AG-11（2026-09-15 审计）：观测必须**有界** —— `read_file` 原先返回整份文件（一章正文可上万字），
多点取证几轮就把上下文顶穿（docs/04 §5.3 的预算/压缩机制并未实现）。此处统一截断并标注。
"""

from __future__ import annotations

from pathlib import Path

from ..core.errors import NOT_FOUND, SCHEMA_FAIL
from ..core.tools import Tool, ToolResult, fail, ok
from ..storage.workspace import Workspace, WorkspaceError

# 单次观测字符上限（拍板：8k 字符/次；`AgentRunner` 侧另设整轮总观测预算）
OBS_LIMIT_CHARS = 8000
GREP_HIT_LIMIT = 100


def _clip(text: str, limit: int = OBS_LIMIT_CHARS) -> tuple[str, bool]:
    """截断超长观测，返回 (文本, 是否被截断)。"""
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n…[已截断：原文 {len(text)} 字符，仅回传前 {limit} 字符]", True


def _resolve(ws: Workspace, rel: str) -> Path:
    return ws._abs(rel)


def tools(ws: Workspace) -> list[Tool]:
    def _read(session, params, budget=None):
        raw = str(params.get("path", "") or "")
        if not raw:
            return fail(SCHEMA_FAIL, {"error": "missing parameter: 'path'"})
        try:
            p = _resolve(ws, raw)
        except WorkspaceError as e:
            return fail(NOT_FOUND, {"error": str(e)})
        if not p.exists():
            return fail(NOT_FOUND, {"path": raw})
        if p.is_dir():
            return ok(data={"path": raw, "entries": sorted(x.name for x in p.iterdir())[:200]})
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as e:
            return fail(NOT_FOUND, {"error": f"{type(e).__name__}: {e}"})
        clipped, was_clipped = _clip(text)
        return ok(data={"content": clipped, "chars": len(text), "truncated": was_clipped})

    def _write(session, params, budget=None):
        raw = str(params.get("path", "") or "")
        if not raw:
            return fail(SCHEMA_FAIL, {"error": "missing parameter: 'path'"})
        try:
            p = _resolve(ws, raw)
        except WorkspaceError as e:
            return fail(NOT_FOUND, {"error": str(e)})
        content = str(params.get("content", ""))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return ok(data={"path": str(p), "bytes": len(content)})

    def _grep(session, params, budget=None):
        needle = str(params.get("pattern", "") or "")
        if not needle:
            return fail(SCHEMA_FAIL, {"error": "missing parameter: 'pattern'"})
        try:
            root = ws._abs(str(params.get("root", "") or ""))
        except WorkspaceError as e:
            return fail(NOT_FOUND, {"error": str(e)})
        hits: list[str] = []
        if root.is_dir():
            for f in root.rglob("*.md"):
                if len(hits) >= GREP_HIT_LIMIT:
                    break
                try:
                    if needle.lower() in f.read_text(encoding="utf-8").lower():
                        hits.append(str(f.relative_to(root)))
                except OSError:  # pragma: no cover - 跳过不可读
                    continue
        return ok(data={"hits": hits, "count": len(hits),
                        "truncated": len(hits) >= GREP_HIT_LIMIT})

    return [
        Tool("read_file", f"读取工作区文件（单次最多回传 {OBS_LIMIT_CHARS} 字符）", "safe", _read,
             {"path": {"type": "string"}}, required=["path"]),
        Tool("write_file", "写入工作区文件（沙箱内）", "safe", _write,
             {"path": {"type": "string"}, "content": {"type": "string"}},
             required=["path", "content"]),
        Tool("grep_text", f"在工作区目录按文本检索（最多 {GREP_HIT_LIMIT} 条命中）", "safe", _grep,
             {"pattern": {"type": "string"}, "root": {"type": "string"}},
             required=["pattern"]),
    ]


__all__ = ["tools", "OBS_LIMIT_CHARS", "GREP_HIT_LIMIT", "ToolResult"]

