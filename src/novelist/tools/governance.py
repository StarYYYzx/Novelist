"""治理工具（docs/05 §4.1 / docs/07 §3）：publish / delete / checkpoint（danger）。

- publish：把已批准章节转正发布（走 promote_draft 语义，danger 门禁）。
- delete：删除草稿/正文文件（dev操作，danger）。
- checkpoint：手动保存检查点（danger）。

全部受 PermissionGate 门禁（默认 danger=deny/ask，docs/02 F6.1）。
"""

from __future__ import annotations

from ..core.session import SessionInfo
from ..core.tools import Tool
from ..storage.checkpoint import Checkpoint
from ..storage.workspace import Workspace, WorkspaceError


def tools(ws: Workspace) -> list[Tool]:
    def _publish(session: SessionInfo, params, budget=None):
        # 把草稿 promote 为正式章节并保存检查点（多danger操作示例）
        try:
            vol, ch = int(params["vol"]), int(params["ch"])
            src = ws.draft_path(session.project_id, vol, ch)
            dst = ws.chapter_path(session.project_id, vol, ch)
            if not src.exists():
                return {"error": "draft not found"}
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            Checkpoint(ws).save(session.project_id, {"id": session.project_id, "pipeline_state": "审查", "event_seq": 0})
            return {"published": str(dst)}
        except (WorkspaceError, ValueError) as e:
            return {"error": str(e)}

    def _delete(session: SessionInfo, params, budget=None):
        try:
            target = params.get("path", "")
            p = ws._abs(target)
            if p.exists():
                if p.is_dir():
                    import shutil

                    shutil.rmtree(p)
                else:
                    p.unlink()
                return {"deleted": str(p)}
            return {"error": "not found"}
        except WorkspaceError as e:
            return {"error": str(e)}

    def _checkpoint(session: SessionInfo, params, budget=None):
        Checkpoint(ws).save(session.project_id, {"id": session.project_id, "_manual": True})
        return {"ok": True}

    return [
        Tool("publish", "发布章节（草稿转正）", "danger", _publish,
             {"vol": {"type": "integer"}, "ch": {"type": "integer"}}),
        Tool("delete_file", "删除工作区文件（沙箱内）", "danger", _delete,
             {"path": {"type": "string"}}),
        Tool("checkpoint", "手动保存检查点", "danger", _checkpoint, {}),
    ]
