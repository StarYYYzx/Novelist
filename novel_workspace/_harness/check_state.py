"""检查并发写是否污染了记忆层与草稿（两个 v2 进程曾同时跑同一批文件）。"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from novelist.core.polish import completeness  # noqa: E402

PID = "proj-duanyu"
BASE = ROOT / "novel_workspace" / PID

events = json.loads((BASE / "memory/plot_events.json").read_text(encoding="utf-8"))
print("=== plot_events ===")
print(f"  总数 {len(events)}（合成 {sum(1 for e in events if e.get('type') == 'chapter')}，"
      f"真实 {sum(1 for e in events if e.get('type') != 'chapter')}）")
per_ch = Counter(f"{(e.get('at') or {}).get('vol')}:{(e.get('at') or {}).get('ch')}" for e in events)
print("  按章分布:", dict(sorted(per_ch.items())))
summaries = [e.get("summary") for e in events]
dup = [s for s, n in Counter(summaries).items() if n > 1]
print(f"  重复摘要 {len(dup)} 条" + (f"：{dup[:3]}" if dup else ""))

print("\n=== character_histories ===")
total_entries = 0
for f in sorted((BASE / "memory/character_histories").glob("*.json")):
    d = json.loads(f.read_text(encoding="utf-8"))
    n = len(d.get("entries") or [])
    total_entries += n
    dups_in = [s for s, c in Counter(x.get("summary") for x in (d.get("entries") or [])).items() if c > 1]
    if dups_in:
        print(f"  {f.stem}: {n} 条，**含重复** {dups_in[:2]}")
print(f"  人物经历总条数 {total_entries}")

print("\n=== 草稿完整性 ===")
drafts = sorted((BASE / "drafts" / "chapters").glob("*.md"))
for f in drafts:
    c = completeness(f.read_text(encoding="utf-8"))
    flag = "ok" if c["ends_properly"] and not c["meta_narration"] else "CHECK"
    print(f"  {f.stem:6s} {c['chars']:5d} 字  {flag}")

idx = json.loads((BASE / "memory/fragment_index.json").read_text(encoding="utf-8"))
sigs = [f["sig"] for f in idx["fragments"]]
print(f"\n=== fragment_index ===\n  碎片 {len(sigs)}，重复 sig {len(sigs) - len(set(sigs))} 条")
