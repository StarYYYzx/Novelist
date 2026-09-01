"""bible 契约校验层（F0'）：磁盘事实源 ↔ JSON Schema 契约对齐（docs/06 §5）。

- `BIBLE_CONTRACT`：相对路径模板 → schema 名。磁盘事实源按此映射逐一过
  `SchemaRegistry.validate`，保证「实然」满足「应然」契约（先契约、后实现，docs/07）。
- `validate_project(ws, project_id)`：遍历契约映射，返回违规清单；**缺失文件不视为违规**
  （加载层对缺失给空值，见 core/context.py `load_bible`），存在才校验。
- `parse_gist(ws, project_id, vol, ch)`：章节细纲 front-matter 宽松解析
  （兼容旧行内 `key_events: [...]` 格式），供 Forge 构建层与校验使用。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..storage.models import SchemaError, SchemaRegistry

# 相对路径模板（可含 `*` 通配，如 memory/character_histories/*.json）→ schema 名
BIBLE_CONTRACT: list[tuple[str, str]] = [
    ("project.json", "project"),
    ("bible/worldview.json", "bible/worldview"),
    ("bible/characters.json", "bible/characters"),
    ("bible/locations.json", "bible/locations"),
    ("bible/items.json", "bible/items"),
    ("bible/settings.json", "bible/settings"),
    ("bible/plot_threads.json", "bible/plot_threads"),
    ("bible/timeline.json", "bible/timeline"),
    ("bible/worldstate.json", "bible/worldstate"),
    ("bible/style.json", "bible/style"),
    ("outline/volumes.json", "outline/volume"),
    ("memory/plot_events.json", "memory/plot_event"),
    ("memory/fragment_index.json", "memory/fragment_index"),
    ("memory/character_histories/*.json", "memory/character_history"),
]


@dataclass
class ContractViolation:
    """一条契约违规：路径 + 对应 schema + 具体错误。"""

    path: str  # 项目内相对路径，如 bible/worldview.json
    schema: str  # schema 名，如 bible/worldview
    errors: list[str] = field(default_factory=list)


def validate_project(ws, project_id: str, *, registry: SchemaRegistry | None = None) -> list[ContractViolation]:
    """校验项目全部契约文件；返回违规清单（空列表 = 全过）。

    缺失文件跳过（加载层以空值兜底）；存在但 JSON 损坏或校验不过 → 记一条违规。
    """
    reg = registry or SchemaRegistry()
    violations: list[ContractViolation] = []
    for rel, schema_name in BIBLE_CONTRACT:
        if "*" in rel:
            base, _, tail = rel.partition("*")
            pat = "*" + tail
            paths = sorted(ws._abs(f"{project_id}/{base}").glob(pat))  # noqa: SLF001
        else:
            p = ws._abs(f"{project_id}/{rel}")  # noqa: SLF001
            paths = [p] if p.exists() else []
        for p in paths:
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError) as e:
                violations.append(ContractViolation(path=str(p), schema=schema_name, errors=[f"读取/解析失败: {e}"]))
                continue
            try:
                reg.validate(schema_name, data)
            except SchemaError as e:
                violations.append(ContractViolation(path=str(p), schema=schema_name, errors=[str(e)]))
    return violations


_GIST_TITLE_RE = re.compile(r"^#\s*第\s*(\d+)\s*章\s*(.*)$", re.M)
_GIST_KEY_EVENTS_RE = re.compile(r"^key_events:\s*\[(.*)\]\s*$", re.M)


def _parse_frontmatter(text: str) -> dict | None:
    """文件头 `---\n{json}\n---` 块 → dict；无块或非 JSON 返回 None。"""
    m = re.match(r"\A---\s*\n(.*?)\n---\s*\n?", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(1).strip())
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def parse_gist(ws, project_id: str, vol: int, ch: int) -> dict | None:
    """解析章节细纲为 `outline/chapter_gist` 契约形状（docs/06 §3.4）。

    支持两种形态：
    1. front-matter 块：文件头 `---\n{json}\n---`（Forge 构建的新格式）。
    2. 旧行内格式：`# 第 N 章 标题` + `key_events: [a, b, c]`（存量细纲）。

    返回 None 当文件缺失。
    """
    p = ws._abs(f"{project_id}/outline/chapters/{vol}-{ch}.md")  # noqa: SLF001
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8")
    fm = _parse_frontmatter(text)
    if fm is not None:
        fm.setdefault("id", f"ch:{vol}:{ch}")
        fm.setdefault("vol", vol)
        fm.setdefault("ch", ch)
        fm.setdefault("turns", [])
        return fm
    gist: dict[str, Any] = {"id": f"ch:{vol}:{ch}", "vol": vol, "ch": ch, "turns": []}
    m = _GIST_TITLE_RE.search(text)
    if m:
        gist["title"] = (m.group(2) or "").strip() or f"第 {ch} 章"
    else:
        gist["title"] = f"第 {ch} 章"
    m = _GIST_KEY_EVENTS_RE.search(text)
    if m:
        events = [piece.strip().strip("'\"").strip() for piece in m.group(1).split(",")]
        events = [e for e in events if e]
        if events:
            gist["key_events"] = events
    return gist
