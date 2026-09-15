"""创作工具（docs/05 §4.1）：write_draft（safe）/ promote_draft（sensitive）。

write_draft 把整章草稿原子写到 drafts/chapters/<vol>-<ch>.md（docs/06 §2 目录规约）。
promote_draft 把草稿转正为 chapters/<vol>-<ch>.md（sensitive，走门禁）。

AG-9（2026-09-15 审计）：失败**不得**包成 `status="ok"` —— 此前参数非法/草稿不存在都返回
`ok(data={"error": ...})`，agent 从 status 上分辨不出成败，会以为自己已经落盘。
"""

from __future__ import annotations

from ..core.errors import NOT_FOUND, SCHEMA_FAIL
from ..core.session import SessionInfo
from ..core.tools import Tool, fail, ok
from ..storage.workspace import Workspace, WorkspaceError


def tools(ws: Workspace) -> list[Tool]:
    def _write_draft(session: SessionInfo, params, budget=None):
        try:
            vol, ch = int(params["vol"]), int(params["ch"])
            content = params["content"]
        except KeyError as e:
            return fail(SCHEMA_FAIL, {"error": f"missing parameter: {e.args[0]!r}"})
        except (TypeError, ValueError) as e:
            return fail(SCHEMA_FAIL, {"error": f"vol/ch 必须是整数：{e}"})
        try:
            p = ws.draft_path(session.project_id, vol, ch)
            p.parent.mkdir(parents=True, exist_ok=True)
            ws.write_text(p, content)
        except WorkspaceError as e:
            return fail(NOT_FOUND, {"error": str(e)})
        return ok(data={"path": str(p), "words": len(content)})

    def _promote_draft(session: SessionInfo, params, budget=None):
        try:
            vol, ch = int(params["vol"]), int(params["ch"])
        except KeyError as e:
            return fail(SCHEMA_FAIL, {"error": f"missing parameter: {e.args[0]!r}"})
        except (TypeError, ValueError) as e:
            return fail(SCHEMA_FAIL, {"error": f"vol/ch 必须是整数：{e}"})
        try:
            src = ws.draft_path(session.project_id, vol, ch)
            dst = ws.chapter_path(session.project_id, vol, ch)
            if not src.exists():
                # 失败就是失败：以前这里返回 ok(data={"error": "draft not found"})
                return fail(NOT_FOUND, {"error": f"draft not found: {src.name}"})
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        except WorkspaceError as e:
            return fail(NOT_FOUND, {"error": str(e)})
        return ok(data={"path": str(dst)})

    return [
        Tool("write_draft", "写入整章草稿（覆盖 drafts/chapters/<卷>-<章>.md）", "safe", _write_draft,
             {"vol": {"type": "integer"}, "ch": {"type": "integer"}, "content": {"type": "string"}},
             required=["vol", "ch", "content"]),
        Tool("promote_draft", "草稿转正为正式章节", "sensitive", _promote_draft,
             {"vol": {"type": "integer"}, "ch": {"type": "integer"}},
             required=["vol", "ch"]),
    ]
