"""B1 批跑探针：yeraln3 1-6 章 18 事件，对拍"广播名单 vs 细纲/文本兜底名单"。

目的：转 True 前的验收证据。对每事件：
- baseline = 确定性路径名单（细纲声明 ∪ 事件文本字面命中）——转 True 前若非广播即用此兜底；
- broadcast 名单（含思考抖动重试，max_tokens 用 orchestrator 同款默认 1600）；
- 判定：degrade(广播未触发→用兜底) / match(广播⊇兜底，零遗漏) / miss(广播遗落兜底人=守卫失守)；
- 增益：broadcast 相对 baseline 新增的在池/注册角色（可能正是"该在场者缺席"的修复信号）。

不污染 canonical：castings 重定向 _harness/.probe_castings/，needs 队列打桩捕获。
用法：python broadcast_batch_probe.py   （需 DeepSeek-API-KEY）
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from novelist.core import broadcast as bc  # noqa: E402
from novelist.core.director import (cast_from_text, load_characters)  # noqa: E402
from novelist.core.orchestrator import parse_cast_decl  # noqa: E402
from novelist.providers.deepseek import DeepSeekProvider  # noqa: E402
from novelist.storage.workspace import Workspace  # noqa: E402

PID = "proj-yelan3"
HERE = Path(__file__).resolve().parent
PROBE_DIR = HERE / ".probe_castings"
OUT = HERE.parent / PID / "reports" / "broadcast_batch_probe.json"
PROBE_DIR.mkdir(parents=True, exist_ok=True)
_probe_needs: list = []


def parse_key_events(gist_text: str) -> list[str]:
    m = re.search(r"key_events:\s*\[(.*)\]", gist_text, re.S)
    if not m:
        return []
    return [p.strip().strip("'\"") for p in m.group(1).split(",") if p.strip()]


def main() -> None:
    provider = DeepSeekProvider()
    ws = Workspace(root=str(ROOT / "novel_workspace"))

    bc._castings_path = lambda ws_, pid_, vol_, ch_, idx_: PROBE_DIR / f"v{vol_}-c{ch_}-e{idx_}.json"
    import novelist.core.character_factory as cf
    cf.queue_need = lambda ws_, pid_, need: _probe_needs.append(need)

    chars = load_characters(ws, PID)
    pool = bc.available_pool(chars, None)
    pool_names = {c["name"] for c in pool}
    registered = {c["name"] for c in chars}

    results = []
    for ch in range(1, 7):
        gist = ws.outline_chapter_path(PID, 1, ch)
        if not gist.exists():
            continue
        evs = parse_key_events(gist.read_text(encoding="utf-8"))
        declared = parse_cast_decl(gist.read_text(encoding="utf-8"))
        for idx, ev_text in enumerate(evs):
            text_hits = [str(c.get("name") or "") for c in cast_from_text(chars, ev_text)]
            baseline = sorted(set(declared) | set(text_hits))
            dec = bc.broadcast_cast(ws, PID, provider, vol=1, ch=ch, idx=idx,
                                    ev_text=ev_text, seam="",
                                    declared=declared, text_hits=text_hits)
            if dec is None:
                verdict = "degrade"
                bcast, reasons, off_scene, alarms = [], [], [], []
            else:
                bcast = dec.names
                off_scene = dec.off_scene_names
                reasons = [f"{m.name}:{m.reason_category}" for m in dec.members]
                alarms = dec.alarms
                miss = sorted(set(baseline) - set(bcast))
                verdict = "miss!" if miss else ("match" if set(bcast) - set(baseline) else "match0")
            added = sorted(set(bcast) - set(baseline)) if dec is not None else []
            rec = {"ch": ch, "ev": ev_text, "baseline": baseline,
                   "broadcast": bcast, "off_scene": off_scene, "verdict": verdict,
                   "added": added, "reasons": reasons, "alarms": alarms}
            results.append(rec)
            print(json.dumps({
                "c%d-e%d" % (ch, idx + 1): verdict, "ev": ev_text[:24],
                "base": baseline, "bc": bcast,
                "added" if verdict in ("match", "match0") else "miss": added or alarms,
            }, ensure_ascii=False), flush=True)

    # 汇总
    total = len(results)
    degrade = sum(1 for r in results if r["verdict"] == "degrade")
    miss0 = sum(1 for r in results if r["verdict"] == "miss!")
    matches = total - degrade - miss0
    additions = sum(len(r["added"]) for r in results)
    # 新增者里"在池或注册"才算合理候选
    plausible_add = sum(1 for r in results for a in r["added"] if a in pool_names or a in registered)
    sum_lines = {
        "total": total, "broadcast_fired": total - degrade, "degrade": degrade,
        "no_drop": total - miss0, "miss": miss0,
        "additions_total": additions, "additions_plausible": plausible_add,
    }
    print("\n---- 汇总 ----", flush=True)
    for k, v in sum_lines.items():
        print(f"  {k}: {v}", flush=True)

    (HERE.parent / PID / "reports").mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"summary": sum_lines, "events": results,
                               "probe_needs": [n.__dict__ for n in _probe_needs]},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nreport: {OUT}", flush=True)


if __name__ == "__main__":
    main()