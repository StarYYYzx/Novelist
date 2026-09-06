"""承诺账本（Covenant）——构建层"恒定 vs 可变"边界（设计定稿 2026-09-06）。

## 它要解决的问题

细纲若允许滚动修订，故事的"走向"会不会失控？承诺账本的答：**防止失控的不是
细纲不许变，而是变更不得触碰承诺**。走向被钉在承诺账本上，细纲只是走向在当下的
执行表——细纲变了，走向没变。

- **恒定层（不可变，仅人工可改）**：已承诺的伏笔及其兑现卷、卷级主线、核心角色
  的人设/弧线/结局，以及已写正文（原则上不重写）。
- **可变层（未来 2-3 章窗口内自由自应）**：key_events 的排序与实现手段、用词/展开。

## 职责

本模块是一个**只读承诺边界视图 + 确定性触碰判定**，**绝不另建同义数据源**（避免
三足鼎立）：它把既有的已承诺数据（蓝图 threads 的 planted/pending_return 伏笔、
volumes 的 threads_to_payoff 主线声明、characters 的核心人设）归一成一列"承诺条目"
供审计与门禁用；`touched_entries` 在**不改动侧**断言：修订草案若触碰承诺条目 →
应由调用方转入 ADR-024 人工审核闸门（`forge.review.mark_pending`），未触碰且仅在
未来窗口内调整 key_events → 可自动落盘。

## 触碰判定原则

- 以**稳定 id**（thread id / volume 号 / character id）定位条目，不用数组下标
  （下标会随蓝图增删漂移，不可作契约锚点）。
- 条目携带 `guard_fields`；某 guard 字段在新旧两份蓝图中取值不同，或条目被删除
  → 判定"触及承诺"。判定全确定性，不依赖 LLM。

本模块不含任何 LLM 调用，纯确定性（docs/09 §2.1 纪律）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .state import Blueprint
from .review import REVIEW_MODULES  # noqa: F401  # 仅用于 source→模块名映射的文档说明

COVENANT_REL = "workspace/forge/covenant.json"

# 核心角色：对它们的角色定位 / 存活状态 / 弧线 / 结局字段改动即视为触及承诺
_CORE_ROLES = {"protagonist", "rival", "love_interest", "mentor"}
# 弧线类字段后缀（玄武区里 key 名不统一，按词尾启发式判定）
_ARC_KEY_SUFFIXES = ("arc", "ending", "fate", "goal", "growth")
_ARC_SUFFIX_RE = re.compile(r"(arc|ending|fate|goal|growth)$", re.IGNORECASE)


# ---- 条目模型 ----
@dataclass
class CovenantEntry:
    """一条不可径改的承诺。source+obj_id 唯一标识一承诺对象。"""

    source: str          # "thread" | "volume" | "character"
    obj_id: str          # 稳定 id（thread.id / volume 号 / character.id）
    kind: str            # 承诺类别标签（如 伏笔兑付 / 卷主线 / 核心人设）
    desc: str            # 人类可读描述
    anchor: str | None   # 兑现坐标（如 "预计 vol 2" / "本卷主线"）
    guard_fields: list[str] = field(default_factory=list)
    provenance: str = "llm"

    @property
    def key(self) -> str:
        return f"{self.source}:{self.obj_id}"


# ---------------------------------------------------------------------------
# 承诺汇聚（只读视图，从蓝图累加，幂等）
# ---------------------------------------------------------------------------

def _is_arc_key(key: str) -> bool:
    return bool(_ARC_SUFFIX_RE.search(str(key).strip()))


def _threads_entries(bp: Blueprint) -> list[CovenantEntry]:
    out: list[CovenantEntry] = []
    for t in bp.data.get("threads") or []:
        status = str(t.get("status") or "")
        if status not in ("planted", "pending_return"):
            continue  # 未承诺（unplanned）/ 已回收（returned）不构成在途承诺
        target = t.get("target_vol")
        anchor = f"预计 vol {target}" if isinstance(target, int) else None
        out.append(CovenantEntry(
            source="thread",
            obj_id=str(t.get("id") or ""),
            kind="伏笔兑付",
            desc=str(t.get("desc") or "(无描述)"),
            anchor=anchor or "待兑现",
            guard_fields=["status", "target_vol"],
            provenance="llm",
        ))
    return out


def _volumes_entries(bp: Blueprint) -> list[CovenantEntry]:
    out: list[CovenantEntry] = []
    for v in bp.data.get("volumes") or []:
        vol = v.get("vol")
        payoff = v.get("threads_to_payoff") or []
        if not payoff:
            continue  # 无兑付声明的卷不构成主线承诺
        out.append(CovenantEntry(
            source="volume",
            obj_id=str(vol),
            kind="卷主线",
            desc=str(v.get("summary") or "(无主线声明)"),
            anchor=f"第 {vol} 卷",
            guard_fields=["threads_to_payoff", "summary"],
            provenance="llm",
        ))
    return out


def _characters_entries(bp: Blueprint) -> list[CovenantEntry]:
    out: list[CovenantEntry] = []
    for c in bp.data.get("characters") or []:
        role = str(c.get("role") or "")
        if role not in _CORE_ROLES:
            continue  # 配角/龙套不构成核心承诺
        guards = ["role", "status"]
        guards += [k for k in c.keys() if _is_arc_key(k)]
        out.append(CovenantEntry(
            source="character",
            obj_id=str(c.get("id") or ""),
            kind="核心人设",
            desc=str(c.get("name") or c.get("id") or "(无名)"),
            anchor=f"角色定位 {role}",
            guard_fields=guards,
            provenance="llm",
        ))
    return out


def build_covenant(bp: Blueprint) -> list[CovenantEntry]:
    """从蓝图汇聚全部承诺条目（确定性、幂等，不含 LLM）。"""
    return _threads_entries(bp) + _volumes_entries(bp) + _characters_entries(bp)


# ---------------------------------------------------------------------------
# 账本文件（审计快照，只读视图落盘；非权威源，blueprint/lines 才是事实源）
# ---------------------------------------------------------------------------

def covenant_path(ws, project_id: str) -> Path:
    return ws._abs(f"{project_id}/{COVENANT_REL}")  # noqa: SLF001


def save_covenant(ws, project_id: str, entries: list[CovenantEntry]) -> Path:
    path = covenant_path(ws, project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "rev": len(entries),
        "entries": [
            {"key": e.key, "source": e.source, "obj_id": e.obj_id, "kind": e.kind,
             "desc": e.desc, "anchor": e.anchor, "guard_fields": e.guard_fields,
             "provenance": e.provenance}
            for e in entries
        ],
    }
    ws.write_json(path, payload)
    return path


def load_covenant(ws, project_id: str, bp: Blueprint | None = None) -> list[CovenantEntry]:
    """读账本快照；快照缺失/损坏则按当前蓝图重建（审计留痕）。"""
    if bp is not None:
        entries = build_covenant(bp)
        save_covenant(ws, project_id, entries)
        return entries
    try:
        data = json.loads(covenant_path(ws, project_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: list[CovenantEntry] = []
    for r in (data.get("entries") or []):
        if not isinstance(r, dict) or not r.get("key"):
            continue
        out.append(CovenantEntry(
            source=r.get("source", ""), obj_id=r.get("obj_id", ""),
            kind=r.get("kind", ""), desc=r.get("desc", ""), anchor=r.get("anchor"),
            guard_fields=list(r.get("guard_fields") or []),
            provenance=r.get("provenance", "llm"),
        ))
    return out


# ---------------------------------------------------------------------------
# 触碰判定（确定性）：新旧蓝图对比，返回被触及的承诺条目
# ---------------------------------------------------------------------------

def _find_item(bp: Blueprint, section: str, obj_id: str) -> dict | None:
    items = bp.data.get(section) or []
    if section == "volumes":
        return next((x for x in items if str(x.get("vol")) == obj_id), None)
    return next((x for x in items if str(x.get("id")) == obj_id), None)


_SECTION_BY_SOURCE = {"thread": "threads", "volume": "volumes",
                      "character": "characters"}


def touched_entries(bp_old: Blueprint, bp_new: Blueprint,
                    entries: list[CovenantEntry] | None = None) -> list[CovenantEntry]:
    """判定正确的最短路径：对每条承诺，取 guard 字段在旧/新蓝图的取值比较。

    - 条目在新蓝图被删除 → 触及承诺；
    - 任一 guard 字段取值变化 → 触及承诺；
    - 其余情况不触及（可在未来窗口内自由修订）。

    全确定性，不依赖 LLM。
    """
    if entries is None:
        entries = build_covenant(bp_old)
    touched: list[CovenantEntry] = []
    for e in entries:
        section = _SECTION_BY_SOURCE.get(e.source)
        if section is None:
            continue
        old_item = _find_item(bp_old, section, e.obj_id)
        new_item = _find_item(bp_new, section, e.obj_id)
        if new_item is None:
            touched.append(e)  # 整条承诺被删除
            continue
        if old_item is not None and any(
            old_item.get(f) != new_item.get(f) for f in e.guard_fields
        ):
            touched.append(e)
    return touched


def affected_modules(touched: list[CovenantEntry]) -> list[str]:
    """被触及承诺 → ADR-024 审核模块 id（source→module 映射，去重保序）。"""
    mapping = {"thread": "threads", "volume": "volumes", "character": "characters"}
    seen: list[str] = []
    for e in touched:
        m = mapping.get(e.source)
        if m and m not in seen:
            seen.append(m)
    return seen


# ---------------------------------------------------------------------------
# 摘要（无 LLM 的人类可读输出）
# ---------------------------------------------------------------------------

def summary_lines(entries: list[CovenantEntry]) -> list[str]:
    lines: list[str] = []
    by_kind: dict[str, list[CovenantEntry]] = {}
    for e in entries:
        by_kind.setdefault(e.kind, []).append(e)
    for kind in ("伏笔兑付", "卷主线", "核心人设"):
        es = by_kind.get(kind) or []
        if not es:
            continue
        lines.append(f"[{kind}] {len(es)} 条")
        for e in es:
            guard = ",".join(e.guard_fields) or "-"
            lines.append(f"  · {e.obj_id}: {e.desc}（{e.anchor}；守卫:{guard}）")
    return lines


__all__ = [
    "CovenantEntry", "COVENANT_REL", "build_covenant", "save_covenant",
    "load_covenant", "touched_entries", "affected_modules", "summary_lines",
]