"""构建前快照与回滚（docs/10 §7.6，M3l F5）：build/roll 前自动 checkpoint（文件轨）。

- `take_snapshot`：把 build/roll 会改写的文件（project.json / bible/ / outline/ /
  blueprint.json / nodes/）复制到 `workspace/forge/snapshots/<时间戳>-<label>/`；
  transcript（追加型审计日志）与 snapshots 自身不入快照。
- `latest_snapshot`：取最近一次快照目录（`forge build --diff` 的比对基线）。
- `restore_snapshot`：整体回退——快照里的文件覆盖回去；快照里没有而现在有的
  文件删除（构建新增产物随回滚消失）。

与 storage/checkpoint.py 的关系：后者是「checksum 校验轨」（只验不存内容），
本模块是真正的内容快照，两者互补（docs/10 §7.6「复用双轨快照」的落地面）。
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from ..storage.workspace import Workspace

SNAPSHOT_REL = "workspace/forge/snapshots"

# 纳入快照的路径（项目内相对路径；目录整棵复制，文件单拷；不存在则跳过）
SNAPSHOT_PATHS: list[str] = [
    "project.json",
    "bible",
    "outline",
    "workspace/forge/blueprint.json",
    "workspace/forge/nodes",
]


def snapshots_dir(ws: Workspace, project_id: str) -> Path:
    return ws._abs(f"{project_id}/{SNAPSHOT_REL}")  # noqa: SLF001


def take_snapshot(ws: Workspace, project_id: str, label: str = "build") -> Path:
    """构建/滚动前打快照；返回快照目录。失败直接抛（快照是回滚的前提，不带病构建）。"""
    dst_root = snapshots_dir(ws, project_id)
    dst_root.mkdir(parents=True, exist_ok=True)
    name = f"{time.strftime('%Y%m%d-%H%M%S')}-{label}"
    dst = dst_root / name
    n = 1
    while dst.exists():  # 同秒重名保护
        n += 1
        dst = dst_root / f"{name}.{n}"
    dst.mkdir()
    project_root = ws._abs(project_id)  # noqa: SLF001
    for rel in SNAPSHOT_PATHS:
        src = project_root / rel
        if not src.exists():
            continue
        target = dst / rel
        if src.is_dir():
            shutil.copytree(src, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
    return dst


def latest_snapshot(ws: Workspace, project_id: str) -> Path | None:
    d = snapshots_dir(ws, project_id)
    if not d.exists():
        return None
    subs = sorted(p for p in d.iterdir() if p.is_dir())
    return subs[-1] if subs else None


def snapshot_blueprint(ws: Workspace, project_id: str, snap: Path | None = None) -> dict | None:
    """读快照内的 blueprint.json（--diff 基线）；无快照/无蓝图返回 None。"""
    if snap is None:
        snap = latest_snapshot(ws, project_id)
    if snap is None:
        return None
    p = snap / "workspace/forge/blueprint.json"
    if not p.exists():
        return None
    import json

    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def restore_snapshot(ws: Workspace, project_id: str, snap: Path | None = None) -> list[str]:
    """回退到指定快照（缺省最近一次）。返回恢复的相对路径列表。

    语义：快照内文件逐个覆盖回项目；当前存在而快照没有的文件（构建新增产物）删除，
    随之清理空目录。project.json 也一并回退（构建期它只动 forge 段）。
    """
    if snap is None:
        snap = latest_snapshot(ws, project_id)
    if snap is None:
        raise FileNotFoundError(f"{project_id}: 无可用快照")
    project_root = ws._abs(project_id)  # noqa: SLF001

    # 1) 快照 → 现场覆盖
    restored: list[str] = []
    for src in sorted(snap.rglob("*")):
        if src.is_dir():
            continue
        rel = src.relative_to(snap).as_posix()
        dst = project_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        restored.append(rel)

    # 2) 快照没有而现在有的 → 删除（构建新增产物）
    snap_files = {p.relative_to(snap).as_posix() for p in snap.rglob("*") if p.is_file()}
    removed: list[str] = []
    for rel in SNAPSHOT_PATHS:
        cur = project_root / rel
        if not cur.exists():
            continue
        if cur.is_file():
            if rel not in snap_files:
                cur.unlink()
                removed.append(rel)
            continue
        for f in sorted(cur.rglob("*")):
            if f.is_file():
                r = f.relative_to(project_root).as_posix()
                if r not in snap_files:
                    f.unlink()
                    removed.append(r)
        # 清理空目录（自底向上）
        for sub in sorted(cur.rglob("*"), reverse=True):
            if sub.is_dir() and not any(sub.iterdir()):
                sub.rmdir()
    return restored
