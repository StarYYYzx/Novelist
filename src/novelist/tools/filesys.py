"""文件系统工具（docs/05 §4.1）：read_file / write_file / grep_text（safe）。

所有读写经 Workspace 沙箱根解析，拒绝越界（docs/06 §7）。Actor 只能写自己的 take 由
权限面/路径约束在调用层保证（此处工具是通用 safe，越权由 SessionInfo + 上层控制）。

AG-11（2026-09-15 审计）：观测必须**有界** —— `read_file` 原先返回整份文件（一章正文可上万字），
多点取证几轮就把上下文顶穿（docs/04 §5.3 的预算/压缩机制并未实现）。此处统一截断并标注。

2026-09-18（批次 A）：
- `write_file` 原定级 `safe`（自动放行）却以**沙箱根**为基准 → 可直接写别的项目、也能绕过
  `publish`/`promote_draft` 的 danger 审批把内容塞进 `chapters/`。现改为：**跨项目写硬拒** +
  分级按目标路径判定（草稿区/围读产物 `safe`，其余 `sensitive` 需审批）。
"""

from __future__ import annotations

from pathlib import Path

from ..core.errors import DENIED, NOT_FOUND, SCHEMA_FAIL
from ..core.tools import LEVEL_SAFE, LEVEL_SENSITIVE, Tool, ToolResult, fail, ok
from ..storage.workspace import Workspace, WorkspaceError

# 单次观测字符上限（拍板：8k 字符/次；`AgentRunner` 侧另设整轮总观测预算）
OBS_LIMIT_CHARS = 8000
GREP_HIT_LIMIT = 100

# 写入自动放行的子目录（相对项目根）——正文草稿与围读产物，属 Agent 本职动作
WRITE_AUTO_ALLOW_SUBDIRS = ("drafts/", "workspace/")


def _clip(text: str, limit: int = OBS_LIMIT_CHARS) -> tuple[str, bool]:
    """截断超长观测，返回 (文本, 是否被截断)。"""
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n…[已截断：原文 {len(text)} 字符，仅回传前 {limit} 字符]", True


def _resolve(ws: Workspace, rel: str, project_id: str | None = None) -> Path:
    """路径解析：给 `project_id` 时以**项目目录**为基准（兼容带项目名前缀的写法）。

    与 `write_draft`/`publish`/`delete_file` 同一口径（2026-09-18）。此前 filesys 三个工具
    以沙箱根为基准，模型照工具描述传 `drafts/x.md` 会落到项目外，与其余工具行为分叉。
    """
    if project_id:
        return ws._abs(f"{project_id}/{_rel_in_project(rel, project_id)}")
    return ws._abs(rel)


def _rel_in_project(raw: str, project_id: str) -> str:
    """把模型给的路径归一成"相对项目根"的形式（兼容带项目名前缀的写法）。

    2026-09-19 审计修复：归一化 `..`——此前 `drafts/../chapters/1-1.md` 能通过
    `startswith("drafts/")` 的白名单/定级检查（定级 safe 自动放行、delete 白名单放行），
    实际落点却逃出白名单目录。含 `..` 段的路径一律硬拒（抛 WorkspaceError）。
    """
    rel = Path(str(raw or "")).as_posix()
    while rel.startswith("./"):
        rel = rel[2:]
    prefix = f"{project_id}/"
    if rel.startswith(prefix):
        rel = rel[len(prefix):]
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise WorkspaceError(f"refuse path with '..': {raw!r}")
    return "/".join(parts)


def tools(ws: Workspace) -> list[Tool]:
    def _read(session, params, budget=None):
        raw = str(params.get("path", "") or "")
        if not raw:
            return fail(SCHEMA_FAIL, {"error": "missing parameter: 'path'"})
        try:
            p = _resolve(ws, raw, session.project_id)
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
            p = _resolve(ws, raw, session.project_id)
            proj = ws.project_dir(session.project_id)
        except WorkspaceError as e:
            return fail(NOT_FOUND, {"error": str(e)})
        # 跨项目写硬拒（2026-09-18）：沙箱根下别的项目、以及根本身一律拒绝
        if not p.is_relative_to(proj) or p == proj:
            return fail(DENIED, {
                "error": "write outside the current project is not allowed",
                "path": raw, "project": session.project_id,
            }, status="denied")
        # 别的项目名前缀：不是"项目内的子目录"，而是基准用错了——明确拒绝而不是造出嵌套目录
        raw_head = Path(str(raw)).as_posix().lstrip("/").split("/", 1)[0]
        if raw_head and raw_head != session.project_id and (
            Path(ws.root) / raw_head / "project.json"
        ).exists():
            return fail(DENIED, {
                "error": ("path is anchored at the current project root; "
                          "cross-project write is not allowed"),
                "path": raw, "project": session.project_id, "other": raw_head,
            }, status="denied")
        content = str(params.get("content", ""))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return ok(data={"path": str(p), "bytes": len(content)})

    def _grep(session, params, budget=None):
        needle = str(params.get("pattern", "") or "")
        if not needle:
            return fail(SCHEMA_FAIL, {"error": "missing parameter: 'pattern'"})
        try:
            root = _resolve(ws, str(params.get("root", "") or ""), session.project_id)
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

    def _write_level(session, params) -> str:
        """写草稿区/围读产物 → safe（自动放行）；其余位置 → sensitive（需审批）。"""
        rel = _rel_in_project(str((params or {}).get("path") or ""), session.project_id)
        if any(rel.startswith(d) for d in WRITE_AUTO_ALLOW_SUBDIRS):
            return LEVEL_SAFE
        return LEVEL_SENSITIVE

    return [
        Tool("read_file", f"读取工作区文件（单次最多回传 {OBS_LIMIT_CHARS} 字符）", "safe", _read,
             {"path": {"type": "string"}}, required=["path"]),
        Tool("write_file",
             "写入**当前项目**内文件（草稿区/围读产物直接写；chapters/、project.json 等需审批）",
             LEVEL_SENSITIVE, _write,
             {"path": {"type": "string"}, "content": {"type": "string"}},
             required=["path", "content"], level_fn=_write_level),
        Tool("grep_text", f"在工作区目录按文本检索（最多 {GREP_HIT_LIMIT} 条命中）", "safe", _grep,
             {"pattern": {"type": "string"}, "root": {"type": "string"}},
             required=["pattern"]),
    ]


__all__ = ["tools", "OBS_LIMIT_CHARS", "GREP_HIT_LIMIT", "ToolResult"]

