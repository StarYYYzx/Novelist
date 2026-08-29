"""文件系统工具（docs/05 §4.1）：read_file / write_file / grep_text（safe）。

所有读写经 Workspace 沙箱根解析，拒绝越界（docs/06 §7）。Actor 只能写自己的 take 由
权限面/路径约束在调用层保证（此处工具是通用 safe，越权由 SessionInfo + 上层控制）。
"""

from __future__ import annotations

from pathlib import Path

from ..core.tools import NOT_FOUND, Tool, ToolResult, ok
from ..storage.workspace import Workspace, WorkspaceError


def _resolve(ws: Workspace, rel: str) -> Path:
    return ws._abs(rel)


def tools(ws: Workspace) -> list[Tool]:
    def _read(session, params, budget=None):
        try:
            p = _resolve(ws, params["path"])
        except WorkspaceError as e:
            return ToolResult(status="error", code=NOT_FOUND, data={"error": str(e)})
        if not p.exists():
            return ToolResult(status="error", code=NOT_FOUND, data={"path": params["path"]})
        return ok(data={"content": p.read_text(encoding="utf-8")})

    def _write(session, params, budget=None):
        try:
            p = _resolve(ws, params["path"])
        except WorkspaceError as e:
            return ToolResult(status="error", code=NOT_FOUND, data={"error": str(e)})
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(params.get("content", ""), encoding="utf-8")
        return ok(data={"path": str(p), "bytes": len(params.get("content", ""))})

    def _grep(session, params, budget=None):
        needle = params.get("pattern", "")
        root = ws._abs(params.get("root", ""))
        hits = []
        if root.is_dir():
            for f in root.rglob("*.md"):
                try:
                    if needle and needle.lower() in f.read_text(encoding="utf-8").lower():
                        hits.append(str(f.relative_to(root)))
                except OSError:  # pragma: no cover - 跳过不可读
                    continue
        return ok(data={"hits": hits})

    return [
        Tool("read_file", "读取工作区文件", "safe", _read, {"path": {"type": "string"}}),
        Tool("write_file", "写入工作区文件（沙箱内）", "safe", _write, {"path": {"type": "string"}, "content": {"type": "string"}}),
        Tool("grep_text", "在工作区目录按文本检索", "safe", _grep, {"pattern": {"type": "string"}, "root": {"type": "string"}}),
    ]
