"""章节重写前置清理（2026-09-05 流程排查 F4/I1-I4 修复）。

## 解决什么

ADR-013 事件实时回写之后，重跑同一章会出现"新旧并存"：plot_events/经历/关系
残留上一轮条目、timeline 时间双倍推进、母题/实体计数翻倍。既有
`memory.rollback_chapter` 定义了但零调用（排查 F4）。

## 设计

- `prepare_rewrite`：写章前按 (vol, ch) 清理记忆层 + 回退时间轴；
  母题账本 / 实体追踪 / tick overdue 的按章幂等已内置在各自模块
  （motif.remove_chapter / entity.chapter_counts / timeline.overdue_by）。
- `ChapterSnapshot`：清理前把受影响 JSON 快照到内存；若生成失败，调用方
  `restore()` 还原，避免"回滚了旧数据又没写进新数据"的中间态。
"""
from __future__ import annotations

_SNAPSHOT_FILES = (
    "memory/plot_events.json",
    "memory/relationships.json",
    "bible/timeline.json",
    "bible/worldstate.json",
)
_SNAPSHOT_DIR = "memory/character_histories"


class ChapterSnapshot:
    """受影响状态文件的内存快照（capture → restore）。"""

    def __init__(self, ws, project_id: str) -> None:
        self._ws = ws
        self._pid = project_id
        self._data: dict[str, bytes | None] = {}
        self._dirs: dict[str, dict[str, bytes | None]] = {}

    def capture(self) -> None:
        for rel in _SNAPSHOT_FILES:
            p = self._ws._abs(f"{self._pid}/{rel}")  # noqa: SLF001 - 与同期存量接口一致
            self._data[rel] = p.read_bytes() if p.exists() else None
        d = self._ws._abs(f"{self._pid}/{_SNAPSHOT_DIR}")  # noqa: SLF001
        files: dict[str, bytes | None] = {}
        if d.is_dir():
            for f in sorted(d.glob("*.json")):
                files[f.name] = f.read_bytes()
        self._dirs[_SNAPSHOT_DIR] = files

    def restore(self) -> int:
        """还原快照。返回恢复的文件数（用于日志）。"""
        n = 0
        for rel, blob in self._data.items():
            p = self._ws._abs(f"{self._pid}/{rel}")  # noqa: SLF001
            if blob is None:
                if p.exists():
                    p.unlink()
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(blob)
            n += 1
        for dirname, files in self._dirs.items():
            d = self._ws._abs(f"{self._pid}/{dirname}")  # noqa: SLF001
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.json")):
                if f.name not in files:
                    f.unlink()
                    n += 1
            for name, blob in files.items():
                if blob is None:
                    continue
                (d / name).write_bytes(blob)
                n += 1
        return n


def prepare_rewrite(ws, project_id: str, vol: int, ch: int, *,
                    embedding=None) -> dict:
    """写章前置清理：按 (vol, ch) 回退上一轮的实时回写。返回各层删除计数。

    调用方应先 `ChapterSnapshot.capture()`，生成失败时 `restore()`。
    """
    from . import timeline
    from .memory import rollback_chapter

    return {
        "memory": rollback_chapter(ws, project_id, vol, ch, embedding=embedding),
        "timeline": timeline.rollback_chapter(ws, project_id, vol, ch),
    }


def snapshot_report(stats: dict) -> str:
    """prepare_rewrite 结果的一行人读摘要（审计用）。"""
    mem = stats.get("memory") or {}
    tl = stats.get("timeline") or {}
    return ("重写前置清理：plot_events -{pe} 经历 -{ex} 关系 -{rel}；"
            "timeline -{tl} 条（回拨 {rw} 天）").format(
        pe=int((mem.get("plot_events") or 0)),
        ex=int((mem.get("experiences") or 0)),
        rel=int((mem.get("relationships") or 0)),
        tl=int(tl.get("removed") or 0), rw=int(tl.get("rewound") or 0))
