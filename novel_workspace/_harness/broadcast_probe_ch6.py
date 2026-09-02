"""ADR-021 广播预演·ch6（只广播，不生成）——真机验收中间步骤。

目的：在 M3r 之后用真实 yelan3 数据回答 ADR-021 验收口径——
"广播名单 vs 确定性名单（细纲声明+文本命中）的差异，是否真的修复了
『职能上该在场的人缺席』"。不重新生成正文（成本 3 次小调用 ≈ 忽略不计）。

方法：对 outline 1-6.md 的 3 个 key_events 重放 orchestrator 传给 broadcast_cast
的输入（真实 bible 池 / parse_cast_decl / cast_from_text / 上一事件出场），
用 DeepSeek 跑广播；对照基准 = ch6 实际落盘的 direction sheet cast
（memory/directions/v1-c6-e*.json，确定性路径的真实结果）。

不污染 canonical 状态：castings 落盘重定向到 _harness/.probe_castings/；
needs 队列打桩捕获不落盘。产出 reports/broadcast_probe_ch6.json + 对照报告。

用法：python broadcast_probe_ch6.py   （需 DeepSeek-API-KEY 环境变量）
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from novelist.core import broadcast as bc  # noqa: E402
from novelist.core.director import (cast_from_text, load_characters,  # noqa: E402
                                    match_cast)
from novelist.core.orchestrator import parse_cast_decl  # noqa: E402
from novelist.providers.deepseek import DeepSeekProvider  # noqa: E402
from novelist.storage.workspace import Workspace  # noqa: E402

PID = "proj-yelan3"
HERE = Path(__file__).resolve().parent
PROBE_DIR = HERE / ".probe_castings"
OUT = HERE.parent / PID / "reports" / "broadcast_probe_ch6.json"
OUT_CASTINGS = []
_probe_needs: list = []


def _parse_key_events(gist_text: str) -> list[str]:
    m = re.search(r"key_events:\s*\[(.*)\]", gist_text, re.S)
    if not m:
        return []
    return [p.strip().strip("'\"") for p in m.group(1).split(",") if p.strip()]


def _direction_cast(ws, ch: int, idx: int) -> list[str]:
    """读 memory/directions/v1-c6-eN.json 的实际 cast（确定性路径真实结果）。"""
    p = ws._abs(f"{PID}/memory/directions/v1-c{ch}-e{idx}.json")
    if not p.exists():
        return []
    d = json.loads(p.read_text(encoding="utf-8"))
    return sorted({c.get("name") for c in (d.get("characters") or []) if c.get("name")})


def _seam(ws, ch: int, idx: int, evs: list[str]) -> str:
    """接缝近似：e1 用上一章草稿尾 300 字（= readback）；e2/e3 用前一事件 key_event 文本。
    （真实运行 seam=上一事件正文尾 300 字；探测标注为近似，prev 名单才是连续性锚点。）"""
    if idx == 0:
        p = ws.draft_path(PID, 1, ch - 1)
        if p.exists():
            return p.read_text(encoding="utf-8")[-300:]
        return ""
    return f"上一事件：{evs[idx - 1]}"


def main() -> None:
    provider = DeepSeekProvider()
    ws = Workspace(root=str(ROOT / "novel_workspace"))
    PROBE_DIR.mkdir(parents=True, exist_ok=True)

    # --- 重定向副作用：不污染 canonical 项目 ---
    def _redirect_casting(ws_, pid_, vol_, ch_, idx_):
        return PROBE_DIR / f"v{vol_}-c{ch_}-e{idx_}.json"

    orig_casting_path = bc._castings_path
    bc._castings_path = _redirect_casting

    import novelist.core.character_factory as cf

    orig_queue = cf.queue_need

    def _probe_queue(ws_, pid_, need):
        _probe_needs.append(need)

    cf.queue_need = _probe_queue

    try:
        chars = load_characters(ws, PID)
        pool = bc.available_pool(chars, None)  # worldstate 无 dead/unavailable（已查）
        gist = ws.outline_chapter_path(PID, 1, 6)
        gist_text = gist.read_text(encoding="utf-8") if gist.exists() else ""
        evs = _parse_key_events(gist_text)
        declared = parse_cast_decl(gist_text)

        results = []
        for idx, ev_text in enumerate(evs):
            text_hits = [str(c.get("name") or "") for c in cast_from_text(chars, ev_text)]
            prev = _direction_cast(ws, 6, idx) if idx > 0 else []
            actual = _direction_cast(ws, 6, idx + 1)  # direction sheet 的 event_index 从 1 起
            baseline = sorted(set(declared) | set(text_hits))
            dec = bc.broadcast_cast(
                ws, PID, provider, vol=1, ch=6, idx=idx,
                ev_text=ev_text, seam=_seam(ws, 6, idx, evs),
                declared=declared, prev=prev, text_hits=text_hits,
                max_tokens=800)
            rec = {
                "event_index": idx + 1, "ev_text": ev_text,
                "pool_size": len(pool),
                "declared": declared, "text_hits": text_hits,
                "deterministic_baseline": baseline,
                "direction_sheet_actual": actual,     # ch6 真实生成时注入的卡
                "prev_event_cast": prev,
                "broadcast_ok": dec is not None,
                "broadcast_names": sorted(dec.names) if dec else [],
                "broadcast_reasons": [{"name": m.name, "category": m.reason_category,
                                       "reason": m.reason} for m in dec.members] if dec else [],
                "needs": [n.__dict__ for n in dec.needs] if dec else [],
                "alarms": dec.alarms if dec else [],
            }
            if dec is not None:
                OUT_CASTINGS.append({"event_index": idx + 1, "raw": dec.raw})
            results.append(rec)
            print(json.dumps({
                "e": idx + 1, "ev": ev_text[:30],
                "actual": actual, "baseline": baseline,
                "broadcast": sorted(rec["broadcast_names"]),
                "alarms": rec["alarms"],
            }, ensure_ascii=False), flush=True)

        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps({
            "probe": "broadcast vs deterministic (ch6, 2026-09-03)",
            "events": results,
            "castings": OUT_CASTINGS,
            "probe_needs": [n.__dict__ for n in _probe_needs],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nreport: {OUT}", flush=True)
        print(f"probe_needs captured: {len(_probe_needs)}（未落盘）", flush=True)
    finally:
        bc._castings_path = orig_casting_path
        cf.queue_need = orig_queue


if __name__ == "__main__":
    main()
