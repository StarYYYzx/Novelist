"""创作工具（docs/05 §4.1）：write_draft（safe）/ promote_draft（sensitive）。

write_draft 把整章草稿原子写到 drafts/chapters/<vol>-<ch>.md（docs/06 §2 目录规约）。
promote_draft 把草稿转正为 chapters/<vol>-<ch>.md（sensitive，走门禁）。
"""

from __future__ import annotations

from ..core.session import SessionInfo
from ..core.tools import Tool, ok
from ..storage.workspace import Workspace, WorkspaceError


def tools(ws: Workspace) -> list[Tool]:
    def _write_draft(session: SessionInfo, params, budget=None):
        try:
            vol, ch = int(params["vol"]), int(params["ch"])
            content = params["content"]
            p = ws.draft_path(session.project_id, vol, ch)
            p.parent.mkdir(parents=True, exist_ok=True)
            ws.write_text(p, content)
            return ok(data={"path": str(p), "words": len(content)})
        except (WorkspaceError, ValueError) as e:
            return ok(data={"error": str(e)})

    def _promote_draft(session: SessionInfo, params, budget=None):
        try:
            vol, ch = int(params["vol"]), int(params["ch"])
            src = ws.draft_path(session.project_id, vol, ch)
            dst = ws.chapter_path(session.project_id, vol, ch)
            if not src.exists():
                return ok(data={"error": "draft not found"})
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            return ok(data={"path": str(dst)})
        except (WorkspaceError, ValueError) as e:
            return ok(data={"error": str(e)})

    return [
        Tool("write_draft", "写入整章草稿", "safe", _write_draft,
             {"vol": {"type": "integer"}, "ch": {"type": "integer"}, "content": {"type": "string"}}),
        Tool("promote_draft", "草稿转正为正式章节", "sensitive", _promote_draft,
             {"vol": {"type": "integer"}, "ch": {"type": "integer"}}),
    ]
