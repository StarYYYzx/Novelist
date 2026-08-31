"""阶段特征实测：逐章扫实体引入密度与设定交代节奏（用于评估"分阶段工作流"设想）。

输出每章：字数 / 本章新实体 / 累计已 established / 预算告警。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from novelist.storage.workspace import Workspace  # noqa: E402
from novelist.core.entity import EntityTracker  # noqa: E402

PID = "proj-yelan"


def main() -> None:
    ws = Workspace(ROOT / "novel_workspace")
    t = EntityTracker.load(ws, PID)
    print(f"实体总数：{len(t.entities)}")
    print(f"{'章':>4} {'字数':>6} {'新实体':>6} {'累计est':>7} {'累计desc+':>8}  告警")
    print("-" * 72)
    for ch in range(1, 21):
        p = ws.chapter_path(PID, 1, ch)
        text = p.read_text(encoding="utf-8") if p.exists() else ""
        upd = t.update_from_chapter(text, 1, ch)
        alerts = t.budget_check(upd["new"])
        est = sum(1 for e in t.entities.values() if e.stage == "established")
        desc = sum(1 for e in t.entities.values()
                   if e.stage in ("described", "established"))
        by_type: dict[str, list[str]] = {}
        for k in upd["new"]:
            e = t.entities[k]
            by_type.setdefault(e.type, []).append(e.name)
        detail = " | ".join(f"{typ}({len(v)}):{'、'.join(v)}"
                            for typ, v in sorted(by_type.items()))
        print(f"{ch:>4} {len(text):>6} {len(upd['new']):>6} {est:>7} {desc:>8}  "
              f"{'超限 ' if alerts else ''}{detail}")


if __name__ == "__main__":
    main()
