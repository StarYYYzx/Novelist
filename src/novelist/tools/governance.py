"""治理工具（docs/05 §4.1 / docs/07 §3）：publish / delete_file / checkpoint（danger）。

- publish：把草稿转正为正式章节（danger 门禁）。
- delete_file：删除草稿区文件（danger + 调用时人工确认 + 路径白名单）。
- checkpoint：手动保存检查点（danger）。

全部受 PermissionGate 门禁（docs/07 §3.3，默认 danger=deny；`delete_file` 默认 ask）。

AG-4 / AG-9 / AG-10（2026-09-15 审计）：
- `checkpoint` / `publish` 此前**整份重写** project.json（`{"id","_manual"}` / `event_seq=0`），
  把 `pipeline_state / event_seq / phase / next / title` 全部抹掉 → 改为**读-改-写合并**。
- 失败值不得包成 ok；返回结构统一为 `ToolResult`。
- `delete_file` 加三层防护：根路径硬拒 → 白名单前缀 → 默认档 ask 人工确认。
"""

from __future__ import annotations

from pathlib import Path

from ..core.errors import DENIED, INTERNAL, NOT_FOUND, SCHEMA_FAIL
from ..core.session import SessionInfo
from ..core.tools import Tool, fail, ok
from ..storage.checkpoint import Checkpoint
from ..storage.workspace import Workspace, WorkspaceError

# 允许删除的路径前缀（相对项目根）——删文件只用于清理草稿/围读产物，不碰 bible/memory/chapters
_DELETE_ALLOWED_PREFIXES = ("drafts/", "workspace/")


def _merge_project_json(ws: Workspace, project_id: str, patch: dict) -> dict:
    """读-改-写合并 project.json（AG-4）：只覆盖 patch 里给出的键。"""
    ck = Checkpoint(ws)
    try:
        current: dict = ck.load(project_id)
    except Exception:  # noqa: BLE001 - 首次/损坏时按空档起步（checkpoint.save 仍会重建）
        current = {}
    current.update(patch)
    ck.save(project_id, current)
    return current


def tools(ws: Workspace) -> list[Tool]:
    def _publish(session: SessionInfo, params, budget=None):
        # 把草稿 promote 为正式章节 + 合并式更新流水线状态（不重置 event_seq）
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
                return fail(NOT_FOUND, {"error": f"draft not found: {src.name}"})
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            _merge_project_json(ws, session.project_id, {"pipeline_state": "审查"})
        except WorkspaceError as e:
            return fail(NOT_FOUND, {"error": str(e)})
        return ok(data={"published": str(dst)})

    def _delete(session: SessionInfo, params, budget=None):
        raw = str(params.get("path", "") or "").strip()
        root_name = session.project_id.rstrip("/")
        # ① 根路径硬拒（AG-10）：`_abs("")` = 沙箱根，`rmtree` 会删掉**整个工作区**
        if not raw or raw.strip("./") == "" or raw.rstrip("/") == root_name:
            return fail(DENIED, {"error": "refuse to delete workspace/project root", "path": raw},
                        status="denied")
        # ② 白名单前缀：只允许删草稿区 / 围读产物
        rel = Path(raw).as_posix()
        while rel.startswith("./"):
            rel = rel[2:]
        rel_in_proj = rel[len(root_name) + 1:] if rel.startswith(root_name + "/") else rel
        if not any(rel_in_proj.startswith(p) for p in _DELETE_ALLOWED_PREFIXES):
            return fail(DENIED, {
                "error": "path not in deletable area (only drafts/ and workspace/)",
                "path": raw, "allowed": list(_DELETE_ALLOWED_PREFIXES),
            }, status="denied")
        try:
            p = ws._abs(raw)
            root_resolved = Path(ws.root).resolve()
            if p == root_resolved or rel_in_proj == "":
                return fail(DENIED, {"error": "refuse to delete root", "path": raw}, status="denied")
            if not p.exists():
                return fail(NOT_FOUND, {"error": "not found", "path": raw})
            if p.is_dir():
                return fail(DENIED, {"error": "directory deletion is not allowed", "path": raw},
                            status="denied")
            p.unlink()
        except WorkspaceError as e:
            return fail(NOT_FOUND, {"error": str(e)})
        except OSError as e:
            return fail(INTERNAL, {"error": f"{type(e).__name__}: {e}"})
        return ok(data={"deleted": str(p)})

    def _checkpoint(session: SessionInfo, params, budget=None):
        # AG-4：合并式写入，绝不整份覆盖（原先 {"id","_manual"} 会抹掉流水线状态）
        current = _merge_project_json(ws, session.project_id,
                                      {"_manual": True, "id": session.project_id})
        return ok(data={"saved": True, "keys": sorted(current)})

    return [
        Tool("publish", "发布章节（草稿转正）", "danger", _publish,
             {"vol": {"type": "integer"}, "ch": {"type": "integer"}},
             required=["vol", "ch"]),
        Tool("delete_file", "删除草稿区文件（仅 drafts/、workspace/ 下；需人工确认）", "danger", _delete,
             {"path": {"type": "string"}}, required=["path"]),
        Tool("checkpoint", "手动保存检查点（合并式更新，不影响既有流水线状态）", "danger", _checkpoint, {}),
    ]
