"""《无双开局》v3 实测：事件循环 + 剧本草稿（重场戏）+ 世界状态层。

每章：事件循环（声明式 key_events，逐事件先忆+接缝+回写）→ 章末文风润色 →
审校师语义检。第 4 章（血刀来袭）标记 heavyweight，启用剧本草稿两遍生成。

规模：5 章 / 11 人物 / 15 key_events（上一轮测试的一半左右）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from novelist.consistency.reviewer import Reviewer  # noqa: E402
from novelist.core.embedding import make_embedding  # noqa: E402
from novelist.core.memory import MemoryIndex, MemoryQuery, MemoryRetriever, rollback_chapter  # noqa: E402
from novelist.core.orchestrator import produce_chapter  # noqa: E402
from novelist.core.session import SessionInfo  # noqa: E402
from novelist.providers.lmstudio import LMStudioProvider  # noqa: E402
from novelist.storage.workspace import Workspace  # noqa: E402

PID = "proj-linfeng"
MODEL = "qwen/qwen3.5-9b"
GEN_TOKENS = 1400   # 事件粒度单次预算（思考 + 正文）
LOG = Path(__file__).resolve().parent / "run_v3_log.jsonl"
LOCK = Path(__file__).resolve().parent / ".v3.lock"

ws = Workspace(root=str(ROOT / "novel_workspace"))


def _acquire_lock() -> None:
    if LOCK.exists():
        raw = LOCK.read_text(encoding="utf-8").strip()
        if raw and raw.isdigit() and _pid_alive(int(raw)):
            raise SystemExit(f"another run active (pid={raw})")
    LOCK.write_text(str(os.getpid()), encoding="utf-8")


def _pid_alive(pid: int) -> bool:
    import subprocess

    try:
        out = subprocess.run(["ps", "-W"], capture_output=True, text=True, timeout=10).stdout
        return any(str(pid) in ln for ln in out.splitlines())
    except Exception:  # noqa: BLE001
        return False


def recall(vol: int, ch: int, embedding, top_k: int = 5) -> list[str]:
    gist = ws.outline_chapter_path(PID, vol, ch)
    gist_text = gist.read_text(encoding="utf-8")[:800] if gist.exists() else ""
    idx = MemoryIndex.load(ws, PID)
    if not idx.fragments:
        idx.rebuild(ws, PID, embedding)
    if not idx.fragments:
        return []
    hits = MemoryRetriever(idx, embedding=embedding).query(
        MemoryQuery(query=gist_text or f"第 {vol} 卷第 {ch} 章", top_k=top_k))
    return [f"- [{h.kind} @ {h.source.get('vol')}:{h.source.get('ch')}] {h.text}" for h in hits if h.score > 0]


def run_chapter(ch: int, provider, embedding) -> dict:
    t0 = time.time()
    sess = SessionInfo(project_id=PID, agent="orchestrator")
    rolled = rollback_chapter(ws, PID, 1, ch)
    memories = recall(1, ch, embedding)

    gist = ws.outline_chapter_path(PID, 1, ch)
    gist_text = gist.read_text(encoding="utf-8") if gist.exists() else ""
    heavyweight = "heavyweight: true" in gist_text

    res = produce_chapter(
        ws, PID, 1, ch, provider, session=sess,
        prefer_direct=True, generation_tokens=GEN_TOKENS, embedding=embedding,
        inject_bible=True, memories=memories or None,
        event_loop=True,                # 事件循环（ADR-013 真落地）
        screenplay=heavyweight,         # 重场戏两遍生成
        polish=True, max_retries=1, commit_chapter_event=None,
    )
    if not res.ok:
        return {"ch": ch, "ok": False, "error": res.result[:200], "secs": round(time.time() - t0)}

    rec = {
        "ch": ch, "ok": True, "secs": round(time.time() - t0),
        "heavyweight": heavyweight,
        "chars": res.completeness.get("chars"),
        "ends_properly": res.completeness.get("ends_properly"),
        "meta": res.completeness.get("meta_narration") or [],
        "bible": res.bible_injected, "attempts": res.attempts,
        "events": res.events_committed,
        "chronicle_written": getattr(res.chronicle, "written", 0),
        "chronicle_conflicts": getattr(res.chronicle, "conflicts", []),
        "state_updates": getattr(res.chronicle, "state_updates", {}),
        "polish_applied": bool(res.polish and res.polish.changed),
        "ai_before": getattr(res.polish, "before", None) and res.polish.before.score,
        "ai_after": getattr(res.polish, "after", None) and res.polish.after.score,
    }

    draft = ws.draft_path(PID, 1, ch)
    reviewer = Reviewer(ws, PID, provider)
    if draft.exists():
        issues = reviewer.review(draft.read_text(encoding="utf-8"), 1, ch,
                                 gist_text=gist_text, memories=memories)
        rec["review"] = [{"level": i.level, "category": i.category, "detail": i.detail} for i in issues]
        rec["review_blocks"] = sum(1 for i in issues if i.level == "block")
    return rec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-ch", type=int, default=1)
    ap.add_argument("--chapters", type=int, default=5)
    args = ap.parse_args()
    _acquire_lock()

    provider = LMStudioProvider(model=MODEL, timeout_s=900,
                                reasoning_aware=True, budget_retries=1)
    embedding = make_embedding("keyword-fallback")
    tally = {"events": 0, "blocks": 0, "truncated": 0, "polished": 0}

    for ch in range(args.from_ch, args.from_ch + args.chapters):
        rec = run_chapter(ch, provider, embedding)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if not rec.get("ok"):
            print(f"[ch{ch}] FAILED {rec.get('error')}", flush=True)
            continue
        tally["events"] += rec.get("events", 0)
        tally["blocks"] += rec.get("review_blocks", 0)
        tally["truncated"] += 0 if rec.get("ends_properly") else 1
        tally["polished"] += 1 if rec.get("polish_applied") else 0
        print(f"[ch{ch}] {rec['secs']}s chars={rec['chars']} 完整={rec['ends_properly']}"
              f" 重场戏={rec['heavyweight']} | 编纂 {rec['chronicle_written']} 条"
              f" 状态更新 {len(rec.get('state_updates') or {})} 人"
              + (f" 冲突{len(rec['chronicle_conflicts'])}" if rec.get("chronicle_conflicts") else "")
              + f" | AI味 {rec.get('ai_before')}→{rec.get('ai_after')}"
              + f" | 审校 block={rec.get('review_blocks', 0)}", flush=True)

    print(f"\nTOTAL {tally}", flush=True)


if __name__ == "__main__":
    main()
