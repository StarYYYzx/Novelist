"""《五五开》20 章收尾：转正 + 全量规则（R-STATE 绑定豁免后复查）+ 设定/伏笔终态 + JIT 统计 + 导出。"""

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
from novelist.core.settings import SettingIndex  # noqa: E402
from novelist.core.tools import PermissionGate  # noqa: E402
from novelist.storage.workspace import Workspace  # noqa: E402
from novelist.tools import build_registry  # noqa: E402

PID = "proj-yelan"
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

    print("\n=== 2) 全量规则（R-STATE 绑定豁免后复查）===")
    alerts = run_consistency(ws, PID)
    print(f"   告警 {len(alerts)} 条")
    for a in alerts[:20]:
        print(f"     [{a.level}/{a.rule_id}] {a.object_ref}: {a.detail[:70]}")

    print("\n=== 3) 设定交代终态 ===")
    idx = SettingIndex.load(ws, PID)
    revealed = sum(1 for e in idx.entries if e.revealed)
    print(f"   {revealed}/{len(idx.entries)} 已交代")
    for e in idx.entries:
        print(f"   {e.id}: revealed={e.revealed}  {'✓' if e.revealed else '✗'}  {e.text[:40]}")

    print("\n=== 4) 伏笔终态（暗线）===")
    threads = json.loads(ws._abs(f"{PID}/bible/plot_threads.json").read_text(encoding="utf-8"))
    for t in threads:
        print(f"   {t['id']}: {t['status']}  {t['desc'][:44]}")

    print("\n=== 5) 人物（含 JIT 补卡统计）===")
    chars = json.loads(ws._abs(f"{PID}/bible/characters.json").read_text(encoding="utf-8"))
    jit = [c for c in chars if str(c.get("id", "")).startswith("char:jit")]
    print(f"   总人物 {len(chars)} | JIT 补卡 {len(jit)}")
    for c in jit[:10]:
        print(f"     {c['name']} (首次 {c.get('first_appear', {}).get('ch')} 章) {c.get('arc', '')[:30]}")

    print("\n=== 6) 每章完整性 / AI 味 / 篇幅 ===")
    total = 0
    for f in sorted((ws.project_dir(PID) / "chapters").glob("*.md")):
        text = f.read_text(encoding="utf-8")
        total += len(text)
        comp = completeness(text)
        m = measure(text)
        flag = "ok" if comp["ends_properly"] and not comp["meta_narration"] else "CHECK"
        print(f"   {f.stem}: {len(text)} 字  {flag}  AI味 {m.score:.1f}")

    print(f"\n=== 7) 导出与统计 ===")
    text = export_project(ws, PID)
    out = ROOT / "novel_workspace" / "_harness" / "五五开.md"
    out.write_text(text, encoding="utf-8")
    s = collect_stats(ws, PID)
    print(f"   发布包: {out} ({len(text)} 字符)")
    print(f"   章节 {s.chapters} | 字数 {s.total_words} | 事件 {s.plot_events} | 人物 {s.characters}")


if __name__ == "__main__":
    main()
