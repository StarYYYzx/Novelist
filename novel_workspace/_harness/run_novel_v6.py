"""《五五开》v6 实测：3080ti 服务器重跑 ch1-10（与 v5/T4 同 bible 同细纲对比）。

后端：云端 3080ti Qwen3.5-9B-Q8（隧道 127.0.0.1:6006，新版 llama-server）——
思考开关有效：
- 正文生成：enable_thinking=False（预算即正文，快）
- 审校：thinking=True + NOVELIST_REVIEWER_TOKENS=4096（判断类开思考，recall 优先）
  —— reviewer.py 默认 800 会被思考吃光致空输出，v6 显式调大
- 重场戏（ch7 大比）：剧本草稿两遍生成（screenplay=True）

embedding：本地 LM-Studio 探测，不可达退化为关键词模式（与 v5 相同）。
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

# 审校预算：开思考时思考占大头，800 必空。3080ti 给 4096。
os.environ.setdefault("NOVELIST_REVIEWER_TOKENS", "4096")

from novelist.consistency.reviewer import Reviewer  # noqa: E402
from novelist.core.embedding import OpenAIEmbedding, make_embedding  # noqa: E402
from novelist.core.memory import MemoryIndex, MemoryQuery, MemoryRetriever, rollback_chapter  # noqa: E402
from novelist.core.orchestrator import produce_chapter  # noqa: E402
from novelist.core.session import SessionInfo  # noqa: E402
from novelist.providers.lmstudio import LMStudioProvider  # noqa: E402
from novelist.storage.workspace import Workspace  # noqa: E402


def _make_embedding():
    import urllib.request

    try:
        with urllib.request.urlopen("http://127.0.0.1:1234/v1/models", timeout=3) as r:
            if r.status == 200:
                return OpenAIEmbedding(
                    model="text-embedding-nomic-embed-text-v1.5",
                    base_url="http://127.0.0.1:1234/v1", api_key="lmstudio")
    except Exception:  # noqa: BLE001
        pass
    print("本地 embedding 不可达 → 关键词检索模式", flush=True)
    return make_embedding("keyword-fallback")

PID = "proj-yelan2"
CLOUD_BASE = "http://127.0.0.1:6006"          # 隧道本地端口（tunnel_autodl.py --server A）
CLOUD_MODEL = "Qwen3.5-9B-Q8_0.gguf"
GEN_TOKENS = 1200                              # 思考已关：预算即正文预算
LENGTH_CAP_CHARS = 7000                        # Y-2 篇幅硬上限（第七批机制接线）：
                                               # 止损极端膨胀（v6 ch9 曾 12023 字），
                                               # 段落边界截断不腰斩；7000 只切失控章，
                                               # 不伤合理长章（v5 ch6 同章 8388 字）
LOG = Path(__file__).resolve().parent / "run_v6_log.jsonl"
HEAVYWEIGHT_CHAPTERS = {7}                     # 1-10 内的重场戏：ch7 大比

ws = Workspace(root=str(ROOT / "novel_workspace"))


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
    heavyweight = ch in HEAVYWEIGHT_CHAPTERS

    res = produce_chapter(
        ws, PID, 1, ch, provider, session=sess,
        prefer_direct=True, generation_tokens=GEN_TOKENS, embedding=embedding,
        inject_bible=True, memories=memories or None,
        event_loop=True, screenplay=heavyweight,
        polish=True, max_retries=1, commit_chapter_event=None,
        knowledge_llm=True, event_review=True,
        event_polish=True, readback=True,
        jit_characters=True, supplement_settings=True,
        length_cap_chars=LENGTH_CAP_CHARS,   # Y-2 篇幅硬上限（第七批机制接线）
    )
    if not res.ok:
        return {"ch": ch, "ok": False, "error": res.result[:200], "secs": round(time.time() - t0)}

    rec = {
        "ch": ch, "ok": True, "secs": round(time.time() - t0),
        "heavyweight": heavyweight,
        "chars": res.completeness.get("chars"),
        "ends_properly": res.completeness.get("ends_properly"),
        "length_truncated": res.length_truncated,  # Y-2 篇幅上限截断标记
        "meta": res.completeness.get("meta_narration") or [],
        "bible": res.bible_injected, "attempts": res.attempts,
        "events": res.events_committed,
        "chronicle_written": getattr(res.chronicle, "written", 0),
        "chronicle_conflicts": getattr(res.chronicle, "conflicts", []),
        "threads_activated": getattr(res.chronicle, "threads_activated", 0),
        "state_updates": getattr(res.chronicle, "state_updates", {}),
        "polish_applied": bool(res.polish and res.polish.changed),
        "ai_before": getattr(res.polish, "before", None) and res.polish.before.score,
        "ai_after": getattr(res.polish, "after", None) and res.polish.after.score,
        "review_blocks": res.review_blocks,
        "events_revised": res.events_revised,
        "lessons_added": res.lessons_added,
        "jit_added": res.jit_added,
        "settings_added": res.settings_added,
    }

    draft = ws.draft_path(PID, 1, ch)
    reviewer = Reviewer(ws, PID, provider)
    if draft.exists():
        try:
            issues = reviewer.review(draft.read_text(encoding="utf-8"), 1, ch,
                                     gist_text=gist_text, memories=memories)
            rec["review"] = [{"level": i.level, "category": i.category, "detail": i.detail} for i in issues]
            rec["review_blocks_chapter"] = sum(1 for i in issues if i.level == "block")
        except Exception as e:  # noqa: BLE001
            # 审校是软环节：正文/编纂/记忆已落盘，审校失败不应让整章计为失败
            rec["review_error"] = f"{type(e).__name__}: {e}"[:200]
            print(f"[ch{ch}] 审校异常（不影响正文）: {rec['review_error']}", flush=True)
    return rec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-ch", type=int, default=1)
    ap.add_argument("--chapters", type=int, default=10)
    args = ap.parse_args()

    provider = LMStudioProvider(base_url=CLOUD_BASE, model=CLOUD_MODEL,
                                enable_thinking=False, timeout_s=600)
    embedding = _make_embedding()
    tally = {"events": 0, "blocks": 0, "revised": 0, "truncated": 0, "polished": 0, "threads": 0}

    for ch in range(args.from_ch, args.from_ch + args.chapters):
        rec = run_chapter(ch, provider, embedding)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if not rec.get("ok"):
            print(f"[ch{ch}] FAILED {rec.get('error')}", flush=True)
            continue
        tally["events"] += rec.get("events", 0)
        tally["blocks"] += rec.get("review_blocks", 0) + rec.get("review_blocks_chapter", 0)
        tally["revised"] += rec.get("events_revised", 0)
        tally["truncated"] += 0 if rec.get("ends_properly") else 1
        tally["polished"] += 1 if rec.get("polish_applied") else 0
        tally["threads"] += rec.get("threads_activated", 0)
        st = rec.get("state_updates") or {}
        print(f"[ch{ch}] {rec['secs']}s chars={rec['chars']} 完整={rec['ends_properly']}"
              f" 重场戏={rec['heavyweight']}"
              + (" 截断!" if rec.get("length_truncated") else "")
              + f" | 编纂 {rec['chronicle_written']} 条"
              f" 状态 {len(st)} 人 伏笔流转 {rec.get('threads_activated', 0)}"
              + (f" 冲突{len(rec['chronicle_conflicts'])}" if rec.get("chronicle_conflicts") else "")
              + f" | 审校 block={rec.get('review_blocks', 0)}/{rec.get('review_blocks_chapter', 0)}"
              + f" 修订 {rec.get('events_revised', 0)} lessons+{rec.get('lessons_added', 0)}"
              + f" | AI味 {rec.get('ai_before')}→{rec.get('ai_after')}", flush=True)

    print(f"\nTOTAL {tally}", flush=True)


if __name__ == "__main__":
    main()
