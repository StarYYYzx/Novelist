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

2026-09-18（批次 A）：
- `delete_file` 此前**校验按"项目内相对路径"、解析却按"沙箱根"**（`ws._abs(raw)`）→ 两个后果：
  模型照工具描述传 `drafts/1-1.md` 永远 NOT_FOUND；传 `<其它项目>/drafts/x.md` 时白名单照样通过
  → 可删别的项目文件。现统一为**按项目目录解析**，校验与解析用同一个 path。
- `_merge_project_json` 此前 `except Exception: current = {}` 后照写 → project.json 损坏/读失败时
  被洗成只含 patch 的空壳（`event_seq`/`title`/`phase` 全丢）。现区分"首次无文件"与"读失败"。
"""

from __future__ import annotations

from pathlib import Path

from ..core.errors import DENIED, INTERNAL, NOT_FOUND, SCHEMA_FAIL
from ..core.session import SessionInfo
from ..core.tools import LEVEL_SENSITIVE, Tool, fail, ok
from ..storage.checkpoint import Checkpoint, CheckpointError
from ..storage.workspace import Workspace, WorkspaceError

# 允许删除的路径前缀（相对项目根）——删文件只用于清理草稿/围读产物，不碰 bible/memory/chapters
_DELETE_ALLOWED_PREFIXES = ("drafts/", "workspace/")


def _strip_project_prefix(raw: str, project_id: str) -> str:
    """归一成"相对项目根"的路径：兼容 `drafts/1-1.md` 与 `<项目>/drafts/1-1.md` 两种写法。

    写入/删除类工具一律以**项目目录**为基准（2026-09-18 拍板），避免"沙箱根"基准下
    模型照工具描述传参却落到项目外、以及跨项目误伤。
    """
    rel = Path(str(raw or "")).as_posix()
    while rel.startswith("./"):
        rel = rel[2:]
    prefix = f"{project_id}/"
    if rel.startswith(prefix):
        rel = rel[len(prefix):]
    # 2026-09-19 审计修复：归一化 `..`——`drafts/../../<别项目>/x` 此前能通过
    # `startswith("drafts/")` 白名单检查（与 filesys._rel_in_project 同口径硬拒）
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise WorkspaceError(f"refuse path with '..': {raw!r}")
    return "/".join(parts)


def _merge_project_json(ws: Workspace, project_id: str, patch: dict) -> tuple[dict | None, str | None]:
    """读-改-写合并 project.json（AG-4）：只覆盖 patch 里给出的键。

    返回 `(合并后的 project, None)` 或 `(None, 错误描述)`。
    **读失败一律不覆写**——`CheckpointError`（无 project.json，属首次）才允许空档起步，
    JSON 损坏/IO 错误必须报错让上层失败，否则会抹掉流水线状态（2026-09-18）。
    """
    ck = Checkpoint(ws)
    try:
        current: dict = ck.load(project_id)
    except CheckpointError:
        current = {}
    except Exception as e:  # noqa: BLE001 - 读失败：拒绝覆写（原因要可读）
        return None, f"{type(e).__name__}: {e}"
    current.update(patch)
    ck.save(project_id, current)
    return current, None


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
            merged, err = _merge_project_json(ws, session.project_id,
                                              {"pipeline_state": "审查"})
            if err:
                return fail(INTERNAL, {"error": f"project.json 读失败，未更新状态：{err}"})
        except WorkspaceError as e:
            return fail(NOT_FOUND, {"error": str(e)})
        return ok(data={"published": str(dst), "project": merged})

    def _delete(session: SessionInfo, params, budget=None):
        raw = str(params.get("path", "") or "").strip()
        root_name = session.project_id.rstrip("/")
        # ① 根路径硬拒（AG-10）：`_abs("")` = 沙箱根，`rmtree` 会删掉**整个工作区**
        if not raw or raw.strip("./") == "" or raw.rstrip("/") == root_name:
            return fail(DENIED, {"error": "refuse to delete workspace/project root", "path": raw},
                        status="denied")
        # ② 白名单前缀：只允许删草稿区 / 围读产物
        try:
            rel_in_proj = _strip_project_prefix(raw, root_name)
        except WorkspaceError:
            return fail(DENIED, {"error": "refuse path with '..'", "path": raw},
                        status="denied")
        if not rel_in_proj or not any(rel_in_proj.startswith(p)
                                      for p in _DELETE_ALLOWED_PREFIXES):
            return fail(DENIED, {
                "error": "path not in deletable area (only drafts/ and workspace/)",
                "path": raw, "allowed": list(_DELETE_ALLOWED_PREFIXES),
            }, status="denied")
        try:
            # 校验与解析必须用同一个 path（2026-09-18）：此前按 rel_in_proj 校验、
            # 却按 raw 相对沙箱根解析，导致描述与实际行为不一致 + 可删别的项目
            p = ws._abs(f"{session.project_id}/{rel_in_proj}")
            root_resolved = Path(ws.root).resolve()
            if p == root_resolved:
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
        current, err = _merge_project_json(ws, session.project_id,
                                          {"_manual": True, "id": session.project_id})
        if err:
            return fail(INTERNAL, {"error": f"project.json 读失败，未写检查点：{err}"})
        return ok(data={"saved": True, "keys": sorted(current)})

    # ---- update_blueprint（M3ac，2026-09-19）----
    # 真机教训：对话 agent 想改设定只能 write_file 整份重写 52KB 的 blueprint.json，
    # 读来读去把轮次与观测预算烧光、两次对话零落地。结构化"按段按字段合并写"是正规通道。
    _BP_DICT_SECTIONS = ("worldview", "style", "meta")
    _BP_LIST_SECTIONS = ("characters", "threads", "lines", "locations", "items",
                         "skills", "settings")

    def _set_dotted(target: dict, key: str, value) -> str:
        """支持点路径（power_system.levels）；返回实际路径。"""
        parts = str(key).split(".")
        node = target
        for p in parts[:-1]:
            nxt = node.get(p)
            if not isinstance(nxt, dict):
                nxt = {}
                node[p] = nxt
            node = nxt
        node[parts[-1]] = value
        return str(key)

    def _update_blueprint(session: SessionInfo, params, budget=None):
        section = str(params.get("section") or "")
        set_fields = params.get("set")
        if not isinstance(set_fields, dict) or not set_fields:
            return fail(SCHEMA_FAIL, {"error": "set 必须是 {'字段': 值} 字典（点路径可嵌套）"})
        if section in _BP_DICT_SECTIONS:
            pass
        elif section in _BP_LIST_SECTIONS or section == "volumes":
            pass
        else:
            return fail(SCHEMA_FAIL, {"error": f"unknown section {section!r}",
                                      "available": list(_BP_DICT_SECTIONS)
                                      + list(_BP_LIST_SECTIONS) + ["volumes"]})
        from ..forge.state import Blueprint

        pid = session.project_id
        try:
            bp = Blueprint.load(ws, pid)
        except (FileNotFoundError, OSError):
            # 项目还没跑过 forge（无蓝图）：从空蓝图起步（与 seed 初始化同口径），
            # 让对话 agent 在项目早期也能落设定（真机：立项期改 worldview 的场景）
            bp = Blueprint.blank()
            bp.data["meta"] = {"title": pid, "genre": "", "logline": "",
                               "scale": {"volumes": 1, "chapters_per_volume": 1,
                                         "target_words_per_chapter": 2400}}
        changed: list[str] = []
        try:
            if section in _BP_DICT_SECTIONS:
                target = bp.data.setdefault(section, {})
                if not isinstance(target, dict):
                    return fail(SCHEMA_FAIL, {"error": f"{section} 不是 dict 段"})
                for k, v in set_fields.items():
                    path = _set_dotted(target, k, v)
                    changed.append(f"{section}.{path}")
                    bp.set_provenance(f"{section}.{path}", "user", 1.0)
            else:
                rows = bp.data.setdefault(section, [])
                if not isinstance(rows, list):
                    return fail(SCHEMA_FAIL, {"error": f"{section} 不是 list 段"})
                if section == "volumes":
                    vol = params.get("id")
                    try:
                        vol_i = int(vol)
                    except (TypeError, ValueError):
                        return fail(SCHEMA_FAIL,
                                    {"error": "volumes 段的 id 是卷号（整数）"})
                    row = next((x for x in rows
                                if int(x.get("vol") or 0) == vol_i), None)
                    if row is None:
                        row = {"vol": vol_i}
                        rows.append(row)
                    rid = f"vol:{vol_i}"
                else:
                    rid = str(params.get("id") or "").strip()
                    if not rid:
                        return fail(SCHEMA_FAIL,
                                    {"error": f"list 段必须给 id（{section} 条目的 id）"})
                    row = bp.find_by_id(section, rid)
                    if row is None:
                        row = {"id": rid}
                        rows.append(row)
                        changed.append(f"{section}[{rid}]（新建条目）")
                for k, v in set_fields.items():
                    row[k] = v
                    changed.append(f"{section}[{rid}].{k}")
                    bp.set_provenance(f"{section}[{rid}].{k}", "user", 1.0)
            bp.save(ws, pid)  # 自带 schema 校验；不过则抛错且不落盘
        except ValueError as e:
            return fail(SCHEMA_FAIL, {"error": f"蓝图校验未过，未落盘：{e}"})
        from ..forge.nodes import sync_bible

        sync_bible(ws, pid, bp)  # 蓝图是应然源，写完同步 bible（读侧立即生效）
        return ok(data={"section": section, "changed": changed, "synced_bible": True})

    return [
        Tool("publish", "发布章节（草稿转正）", "danger", _publish,
             {"vol": {"type": "integer"}, "ch": {"type": "integer"}},
             required=["vol", "ch"]),
        Tool("delete_file",
             "删除当前项目的草稿区文件（路径相对项目根，仅 drafts/、workspace/ 下；需人工确认）",
             "danger", _delete,
             {"path": {"type": "string"}}, required=["path"]),
        Tool("checkpoint", "手动保存检查点（合并式更新，不影响既有流水线状态）", "danger", _checkpoint, {}),
        Tool("update_blueprint",
             "改蓝图设定（dict 段 worldview/style/meta 按键合并，支持点路径如 "
             "power_system.levels；list 段按 id 合并条目字段，volumes 段按卷号）。"
             "保存后自动同步 bible——改设定的正规通道，不要整文件重写 JSON",
             LEVEL_SENSITIVE, _update_blueprint,
             {"section": {"type": "string"}, "id": {"type": "string"},
              "set": {"type": "object"}},
             required=["section", "set"]),
    ]
