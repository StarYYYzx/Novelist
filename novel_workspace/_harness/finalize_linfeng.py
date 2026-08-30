"""《无双开局》收尾：转正 + 全量规则（含 R-STATE）+ 导出 + 指标。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from novelist.consistency import run_consistency  # noqa: E402
from novelist.core.export import collect_stats, export_project  # noqa: E402
from novelist.core.polish import completeness, measure  # noqa: E402
from novelist.core.session import SessionInfo  # noqa: E402
from novelist.core.tools import PermissionGate  # noqa: E402
from novelist.storage.workspace import Workspace  # noqa: E402
from novelist.tools import build_registry  # noqa: E402

PID = "proj-linfeng"
ws = Workspace(root=str(ROOT / "novel_workspace"))
sess = SessionInfo(project_id=PID, agent="orchestrator")


def promote_all() -> int:
    gate = PermissionGate(profiles={"supervised": {"sensitive": "allow", "danger": "deny", "tools": {}}})
    reg = build_registry(ws, gate=gate)
    n = 0
    for f in sorted((ws.project_dir(PID) / "drafts" / "chapters").glob("*.md")):
        v, c = f.stem.split("-")
        if reg.invoke(sess, "promote_draft", {"vol": int(v), "ch": int(c)}).status == "ok":
            n += 1
    return n


def main() -> None:
    print("=== 1) 转正 ===")
    print(f"   promoted {promote_all()} 章")

    print("\n=== 2) 全量规则（含 R-STATE / R-LEX / R-PWR / R-SEM 之外）===")
    alerts = run_consistency(ws, PID)
    print(f"   告警 {len(alerts)} 条")
    for a in alerts[:15]:
        print(f"     [{a.level}/{a.rule_id}] {a.object_ref}: {a.detail[:70]}")

    print("\n=== 3) 世界状态终版 ===")
    st = json.loads(ws._abs(f"{PID}/bible/worldstate.json").read_text(encoding="utf-8"))
    for cid, c in st["characters"].items():
        bits = [f"{c.get('name')}"]
        if c.get("realm"): bits.append(c["realm"])
        if c.get("location"): bits.append(c["location"])
        if c.get("items"): bits.append("持:" + "、".join(c["items"][:3]))
        if c.get("injuries"): bits.append("伤:" + "、".join(c["injuries"]))
        if c.get("dead"): bits.append("【已亡】")
        print("   " + " ".join(bits))

    print("\n=== 4) 每章完整性 / AI 味 ===")
    total = 0
    for f in sorted((ws.project_dir(PID) / "chapters").glob("*.md")):
        text = f.read_text(encoding="utf-8")
        total += len(text)
        comp = completeness(text)
        m = measure(text)
        flag = "ok" if comp["ends_properly"] and not comp["meta_narration"] else "CHECK"
        print(f"   {f.stem}: {len(text)} 字  {flag}  AI味 {m.score}")

    print(f"\n=== 5) 导出与统计 ===")
    text = export_project(ws, PID)
    out = ROOT / "novel_workspace" / "_harness" / "无双开局.md"
    out.write_text(text, encoding="utf-8")
    s = collect_stats(ws, PID)
    print(f"   发布包: {out} ({len(text)} 字符)")
    print(f"   章节 {s.chapters} | 字数 {s.total_words} | 事件 {s.plot_events} | 人物 {s.characters}")


if __name__ == "__main__":
    main()
