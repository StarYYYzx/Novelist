"""导出发布包与统计（docs/02 F8，F8.1/F8.2）。

- `export_project`：把已发布章节汇总为单个 Markdown 文本（发布包）。
- `stats`：字数/章节数/事件数/一致性告警分布 等统计。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..storage.workspace import Workspace


@dataclass
class ProjectStats:
    chapters: int = 0
    drafts: int = 0
    total_words: int = 0
    plot_events: int = 0
    characters: int = 0
    consistency_alerts: int = 0


def _book_title(ws: Workspace, project_id: str) -> str:
    """发布包书名：优先 project.json.title，回退 project_id。

    原先直接用 project_id，导出来是「# proj-duanyu」这种机器名（B-13）。
    """
    try:
        data = ws.read_json(project_id, ws.project_json_path(project_id), required=False)
        title = (data or {}).get("title") if isinstance(data, dict) else None
        if title:
            return str(title)
    except Exception:  # noqa: BLE001 - project.json 缺失/损坏时用 id 兜底
        pass
    return project_id


def export_project(ws: Workspace, project_id: str, *, include_drafts: bool = False) -> str:
    """导出发布包：拼接已发布章节正文（docs/02 F8.1）。"""
    root = ws.project_dir(project_id)
    chapters_dir = root / "chapters"
    parts = [f"# {_book_title(ws, project_id)}\n"]
    if chapters_dir.is_dir():
        for f in sorted(chapters_dir.glob("*.md")):
            parts.append(f"\n## {f.stem}\n")
            parts.append(f.read_text(encoding="utf-8"))
    if include_drafts:
        drafts_dir = root / "drafts" / "chapters"
        if drafts_dir.is_dir():
            parts.append("\n## 草稿\n")
            for f in sorted(drafts_dir.glob("*.md")):
                parts.append(f"\n### {f.stem} (draft)\n")
                parts.append(f.read_text(encoding="utf-8"))
    return "\n".join(parts)


def collect_stats(ws: Workspace, project_id: str) -> ProjectStats:
    """收集项目统计（docs/02 F8.2）。"""
    import json

    stats = ProjectStats()
    root = ws.project_dir(project_id)

    chapters_dir = root / "chapters"
    if chapters_dir.is_dir():
        for f in chapters_dir.glob("*.md"):
            stats.chapters += 1
            stats.total_words += len(f.read_text(encoding="utf-8"))

    drafts_dir = root / "drafts" / "chapters"
    if drafts_dir.is_dir():
        stats.drafts = len(list(drafts_dir.glob("*.md")))

    mem = root / "memory" / "plot_events.json"
    if mem.exists():
        try:
            stats.plot_events = len(json.loads(mem.read_text(encoding="utf-8")))
        except ValueError:  # pragma: no cover
            pass

    bible = root / "bible" / "characters.json"
    if bible.exists():
        try:
            stats.characters = len(json.loads(bible.read_text(encoding="utf-8")))
        except ValueError:  # pragma: no cover
            pass
    return stats
