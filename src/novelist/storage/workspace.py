"""存储层：工作区读写、schema 校验、SQLite 辅助索引（docs/06，ADR-016）。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Workspace:
    """小说工作区（docs/06 §2 目录规约）。路径均相对沙箱根解析。"""

    root: str

    def project_dir(self, project_id: str) -> str:
        # 简单拼接；沙箱越界校验在 tool 层（docs/06 §7）统一做
        return f"{self.root}/{project_id}"

    def bible_path(self, project_id: str, name: str) -> str:
        return f"{self.project_dir(project_id)}/bible/{name}.json"

    def memory_dir(self, project_id: str) -> str:
        return f"{self.project_dir(project_id)}/memory"

    def outline_dir(self, project_id: str) -> str:
        return f"{self.project_dir(project_id)}/outline/chapters"

    def draft_path(self, project_id: str, vol: int, ch: int) -> str:
        return f"{self.outline_dir(project_id)}/{vol}-{ch}.md"
