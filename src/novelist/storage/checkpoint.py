"""存储层：检查点与恢复（docs/06 §6，ADR-010 / §9）。

双轨：
- 版本轨：bible/outline/memory 等为 Git 可 diff 文本文件，改动即有历史。
- 快照轨：project.json（流水线状态指针）+ .checksum.json（关键文件 hash），用于程序级恢复。

恢复逻辑：project.json + .checksum.json 校验一致 -> 重建状态；不一致 -> 报不一致点（由上层决定以 Git/日志回溯）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .workspace import Workspace

# 需要纳入 checksum 的关键文件（相对项目根）（docs/06 §6）
CHECKSUM_FILES = [
    "project.json",
    "bible/worldview.json",
    "bible/characters.json",
    "bible/locations.json",
    "bible/timeline.json",
    "bible/plot_threads.json",
    "bible/style.json",
    "outline/volumes.json",
    "memory/plot_events.json",
    "memory/relationships.json",
    "memory/fragment_index.json",
]


class CheckpointError(Exception):
    pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class Checkpoint:
    """项目检查点：写/读 project.json 与 .checksum.json。"""

    def __init__(self, ws: Workspace) -> None:
        self.ws = ws

    # ---- 写 ----
    def save(self, project_id: str, project: dict) -> dict:
        """写入 project.json，并据此更新 .checksum.json。返回 checksum 映射。"""
        pj_path = self.ws.project_json_path(project_id)
        self.ws.write_json(pj_path, project)

        checksums: dict[str, str] = {}
        rel_root = Path(project_id)
        for rel in CHECKSUM_FILES:
            p = self.ws._abs(str(rel_root / rel))
            if p.exists():
                checksums[rel] = _sha256(p)
        # 额外扫描 bible 下所有实体文件（含新增，不限于预置清单）
        bible_dir = self.ws._abs(str(rel_root / "bible"))
        if bible_dir.exists():
            for f in sorted(bible_dir.glob("*.json")):
                checksums["bible/" + f.name] = _sha256(f)

        self.ws.write_json(self.ws.checksum_path(project_id), checksums)
        return checksums

    # ---- 读 / 恢复 ----
    def load(self, project_id: str) -> dict:
        """读取 project.json；缺失则抛错。"""
        pj_path = self.ws.project_json_path(project_id)
        if not pj_path.exists():
            raise CheckpointError(f"no project.json for {project_id}")
        return dict(self.ws.read_json(project_id, pj_path))

    def verify(self, project_id: str) -> list[str]:
        """校验 .checksum.json 与当前文件是否一致；返回不一致条目的 rel 路径列表（空表示一致）。"""
        ck_path = self.ws.checksum_path(project_id)
        if not ck_path.exists():
            return ["<no .checksum.json>"]
        recorded = self.ws.read_json(project_id, ck_path)
        mismatches: list[str] = []
        rel_root = Path(project_id)
        for rel, expected in recorded.items():
            p = self.ws._abs(str(rel_root / rel))
            if not p.exists():
                mismatches.append(rel)
            elif _sha256(p) != expected:
                mismatches.append(rel)
        return mismatches

    def restore(self, project_id: str) -> dict:
        """读取 project.json 并校验 checksum；不一致抛 CheckpointError。"""
        project = self.load(project_id)
        mismatches = self.verify(project_id)
        if mismatches:
            raise CheckpointError(
                f"project {project_id} file checksum changed for: {', '.join(mismatches)}"
            )
        return project
