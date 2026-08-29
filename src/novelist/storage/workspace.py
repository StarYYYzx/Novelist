"""存储层：小说工作区（docs/06 §2 目录规约，ADR-004/016）。

工作区是系统长期状态的"单一事实源"（文件即状态）：
- bible/  设定圣经（权威·应然）markdown/json
- outline/ 大纲（卷/细纲）
- drafts/chapters 正文草稿与正式章节
- memory/ 记忆层（事实·实然）
- takes/  演员试演（临时）
- reports/ 审查与统计
- .index.db SQLite 辅助索引（docs/06 §9，ADR-016）
- project.json 项目元数据 + 流水线状态
- .checksum.json 关键文件 hash 索引

沙箱约束：所有读写路径必须解析到沙箱根内，拒绝 `..` 越界与绝对路径越界（docs/06 §7）。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .indexdb import IndexDb

# 工作区相对根目录的子目录（docs/06 §2）
SUBDIRS = [
    "bible",
    "outline",
    "outline/chapters",
    "drafts",
    "drafts/chapters",
    "chapters",
    "workspace",
    "memory",
    "memory/character_histories",
    "memory/rag",
    "takes",
    "reports/alerts",
    "reports/stats",
    "logs",
]

SANDBOX_OUTSIDE = "path outside sandbox root"
PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class WorkspaceError(Exception):
    pass


@dataclass
class Workspace:
    """小说工作区（docs/06 §2）。路径均相对沙箱根解析。"""

    root: str = "."

    def _abs(self, rel: str) -> Path:
        """把相对根路径解析为绝对路径，并强制在沙箱根内（防 `..` 越界）。"""
        root = Path(self.root).resolve()
        p = (root / rel).resolve()
        if not p.is_relative_to(root):
            raise WorkspaceError(SANDBOX_OUTSIDE)
        return p

    # ---- 项目目录 ----
    def project_dir(self, project_id: str) -> Path:
        if not PROJECT_ID_RE.match(project_id):
            raise WorkspaceError(f"invalid project_id: {project_id!r}")
        return self._abs(project_id)

    def create_project(self, project_id: str) -> Path:
        """新建项目工作区骨架（docs/07 §6.1 init，UC-01）。"""
        root = self.project_dir(project_id)
        root.mkdir(parents=True, exist_ok=True)
        for sub in SUBDIRS:
            (root / sub).mkdir(parents=True, exist_ok=True)
        # 空 bible/outline 占位文件写进初态 project.json（在 checkpoint 里做）
        return root

    def exists(self, project_id: str) -> bool:
        return self.project_dir(project_id).exists()

    # ---- 路径访问器 ----
    def bible_path(self, project_id: str, name: str) -> Path:
        return self._abs(f"{project_id}/bible/{name}.json")

    def outline_volumes_path(self, project_id: str) -> Path:
        return self._abs(f"{project_id}/outline/volumes.json")

    def outline_chapter_path(self, project_id: str, vol: int, ch: int) -> Path:
        return self._abs(f"{project_id}/outline/chapters/{vol}-{ch}.md")

    def draft_path(self, project_id: str, vol: int, ch: int) -> Path:
        return self._abs(f"{project_id}/drafts/chapters/{vol}-{ch}.md")

    def chapter_path(self, project_id: str, vol: int, ch: int) -> Path:
        return self._abs(f"{project_id}/chapters/{vol}-{ch}.md")

    def memory_dir(self, project_id: str) -> Path:
        return self._abs(f"{project_id}/memory")

    def char_history_path(self, project_id: str, char_id: str) -> Path:
        # 仅允许合法字符组成文件名；防路径注入
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", char_id)
        return self._abs(f"{project_id}/memory/character_histories/{safe}.json")

    def relationships_path(self, project_id: str) -> Path:
        return self._abs(f"{project_id}/memory/relationships.json")

    def fragment_index_path(self, project_id: str) -> Path:
        return self._abs(f"{project_id}/memory/fragment_index.json")

    def rag_dir(self, project_id: str) -> Path:
        return self._abs(f"{project_id}/memory/rag")

    def project_json_path(self, project_id: str) -> Path:
        return self._abs(f"{project_id}/project.json")

    def checksum_path(self, project_id: str) -> Path:
        return self._abs(f"{project_id}/.checksum.json")

    def index_db_path(self, project_id: str) -> Path:
        return self._abs(f"{project_id}/.index.db")

    def logs_path(self, project_id: str) -> Path:
        return self._abs(f"{project_id}/logs")

    # ---- 文件读写（原子写 / UTF-8）----
    def read_json(self, project_id: str, path: Path, *, required: bool = True) -> dict | list:
        if not path.exists():
            if required:
                raise WorkspaceError(f"missing file: {path}")
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            raise WorkspaceError(f"invalid json {path}: {e}") from e

    def write_json(self, path: Path, data) -> None:
        """原子写 JSON（临时文件 + rename，docs/06 §7）。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def read_text(self, project_id: str, path: Path, *, required: bool = False) -> str:
        if not path.exists():
            if required:
                raise WorkspaceError(f"missing file: {path}")
            return ""
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def write_text(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    # ---- SQLite 辅助索引（ADR-016）----
    def index_db(self, project_id: str) -> IndexDb:
        db = IndexDb(str(self.index_db_path(project_id)))
        db.init()
        return db
