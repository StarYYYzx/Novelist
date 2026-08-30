"""文风与质量度量的快照与对比（v1 vs v2）。

用途：用**确定性指标**证明新链路（圣经注入 + 完整性校验 + 编纂员 + 审校 + 文风润色）
确实比旧链路更好，而不是"感觉更好"。

用法：
    python novel_workspace/_harness/metrics.py snapshot v1   # 拍当前 chapters/ 的指标
    python novel_workspace/_harness/metrics.py compare       # 对比 v1 / v2
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from novelist.consistency import run_consistency  # noqa: E402
from novelist.core.polish import completeness, measure  # noqa: E402
from novelist.storage.workspace import Workspace  # noqa: E402

PID = "proj-duanyu"
WS = ROOT / "novel_workspace"
OUT = Path(__file__).resolve().parent


def snapshot(tag: str) -> dict:
    ws = Workspace(root=str(WS))
    chapters_dir = ws._abs(f"{PID}/chapters")
    files = sorted(chapters_dir.glob("*.md")) if chapters_dir.is_dir() else []
    if not files:
        raise SystemExit("no chapters to measure")

    per_chapter = {}
    all_text = []
    total_chars = 0
    truncated = []
    meta_leak = []
    for f in files:
        text = f.read_text(encoding="utf-8")
        all_text.append(text)
        total_chars += len(text)
        m = measure(text)
        c = completeness(text)
        per_chapter[f.stem] = {
            "chars": len(text),
            "ai_tone": m.score,
            "paragraphs": m.paragraphs,
            "para_stdev": m.para_len_stdev,
            "signals": m.signals,
            "ends_properly": c["ends_properly"],
            "meta_narration": c["meta_narration"],
        }
        if not c["ends_properly"]:
            truncated.append(f.stem)
        if c["meta_narration"]:
            meta_leak.append(f.stem)

    joined = "\n".join(all_text)
    overall = measure(joined)
    alerts = run_consistency(ws, PID)

    events = json.loads(ws._abs(f"{PID}/memory/plot_events.json").read_text(encoding="utf-8")) \
        if ws._abs(f"{PID}/memory/plot_events.json").exists() else []
    real = [e for e in events if e.get("type") != "chapter"]
    synthetic = [e for e in events if e.get("type") == "chapter"]

    snap = {
        "tag": tag,
        "chapters": len(files),
        "total_chars": total_chars,
        "ai_tone_overall": overall.score,
        "ai_tone_avg": round(sum(v["ai_tone"] for v in per_chapter.values()) / len(per_chapter), 2),
        "truncated_chapters": truncated,
        "meta_narration_chapters": meta_leak,
        "consistency_alerts": len(alerts),
        "alerts_by_rule": _by_rule(alerts),
        "events_total": len(events),
        "events_real": len(real),
        "events_synthetic": len(synthetic),
        "per_chapter": per_chapter,
    }
    (OUT / f"metrics_{tag}.json").write_text(
        json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    return snap


def _by_rule(alerts) -> dict:
    out: dict[str, int] = {}
    for a in alerts:
        out[a.rule_id] = out.get(a.rule_id, 0) + 1
    return out


def _show(s: dict) -> None:
    print(f"[{s['tag']}] 章节 {s['chapters']} | 字数 {s['total_chars']} "
          f"| AI味 总体 {s['ai_tone_overall']} 均值 {s['ai_tone_avg']}")
    print(f"   截断 {s['truncated_chapters'] or '无'} | 元叙事 {s['meta_narration_chapters'] or '无'}")
    print(f"   一致性告警 {s['consistency_alerts']} {s['alerts_by_rule'] or ''}")
    print(f"   事件 总 {s['events_total']} = 真实 {s['events_real']} + 合成 {s['events_synthetic']}")


def compare(a_tag: str = "v1", b_tag: str = "v2") -> None:
    pa, pb = OUT / f"metrics_{a_tag}.json", OUT / f"metrics_{b_tag}.json"
    if not pa.exists() or not pb.exists():
        raise SystemExit(f"missing snapshot: {pa.name} / {pb.name}")
    a, b = json.loads(pa.read_text(encoding="utf-8")), json.loads(pb.read_text(encoding="utf-8"))
    _show(a)
    _show(b)
    print()
    print("=== 对比（v2 - v1，负数=变好）===")
    rows = [
        ("AI 味总分", a["ai_tone_overall"], b["ai_tone_overall"]),
        ("AI 味均值", a["ai_tone_avg"], b["ai_tone_avg"]),
        ("总字数", a["total_chars"], b["total_chars"]),
        ("截断章节数", len(a["truncated_chapters"]), len(b["truncated_chapters"])),
        ("元叙事章节数", len(a["meta_narration_chapters"]), len(b["meta_narration_chapters"])),
        ("一致性告警数", a["consistency_alerts"], b["consistency_alerts"]),
        ("真实事件数", a["events_real"], b["events_real"]),
        ("合成事件数", a["events_synthetic"], b["events_synthetic"]),
    ]
    for name, x, y in rows:
        delta = round(y - x, 2)
        arrow = "↓" if delta < 0 else ("↑" if delta > 0 else "=")
        print(f"  {name:14s} {x:>8} → {y:>8}   {delta:+.2f} {arrow}")

    print("\n=== 各章 AI 味 ===")
    for k in sorted(a["per_chapter"]):
        av = a["per_chapter"].get(k, {}).get("ai_tone")
        bv = b["per_chapter"].get(k, {}).get("ai_tone")
        if av is None or bv is None:
            continue
        print(f"  {k:6s} {av:>6} → {bv:>6}  {bv - av:+.2f}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "snapshot"
    if cmd == "snapshot":
        _show(snapshot(sys.argv[2] if len(sys.argv) > 2 else "v1"))
    else:
        compare(sys.argv[2] if len(sys.argv) > 2 else "v1",
                sys.argv[3] if len(sys.argv) > 3 else "v2")
