"""《五五开》v7 实测：服务器 C（bjb2/3080 Ti，FreeToken）Qwen3.6-35B-A3B-FP8 跑 ch1-5。

与 v6（proj-yelan2 / Qwen3.5-9B @3080ti llama-server）同 bible 同细纲对比。

后端：ft serve（OpenAI 兼容，隧道 127.0.0.1:18006 → 远端 6006，tunnel_autodl.py --server C）
- 思考控制：reasoning_effort 协议（FreeToken 硬约束，必须显式传）：
  - 正文/调度/视角/拟题/润色：reasoning_effort="none"（预算即正文，快）
  - 审校：请求级 thinking=True → provider 自动升 "low" + NOVELIST_REVIEWER_TOKENS=4096
- ADR-020 四件套默认全开（defer_title / cast_injection / character_direction / perspective_memory）
- embedding：本地 LM-Studio 探测，不可达退化为关键词模式（与 v6 相同）

用法：先起隧道（python tunnel_autodl.py --server C），再 python run_novel_v7.py [--chapters 5]
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

# 审校预算：thinking=low 时思考占大头，800 必空（v6 教训），给 4096。
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

    # 优先云端 embedding（服务器 C llama-server --embeddings，隧道 18010；
    # 2026-09-03 起 LLM+embedding 全部上云，本地 LM-Studio 仅作后备）
    for url, model in (("http://127.0.0.1:18010/v1", "nomic-embed-text-v1.5"),
                       ("http://127.0.0.1:1234/v1", "text-embedding-nomic-embed-text-v1.5")):
        try:
            req = urllib.request.Request(
                f"{url}/embeddings",
                data=json.dumps({"model": model, "input": "探测"}).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=4) as r:
                if r.status == 200:
                    which = "云端" if "18010" in url else "本地"
                    print(f"embedding: {which} nomic（{url}）", flush=True)
                    return OpenAIEmbedding(model=model, base_url=url, api_key="probe")
        except Exception:  # noqa: BLE001
            continue
    print("embedding 不可达 → 关键词检索模式", flush=True)
    return make_embedding("keyword-fallback")

PID = "proj-yelan3"
CLOUD_BASE = "http://127.0.0.1:18006"          # 隧道本地端口（tunnel_autodl.py --server C）
CLOUD_MODEL = "qwen3.6-35b-a3b-fp8"
GEN_TOKENS = 1200                              # reasoning_effort=none：预算即正文预算
LENGTH_CAP_CHARS = 7000                        # Y-2 篇幅硬上限（与 v6 一致便于对比）
LOG = Path(__file__).resolve().parent / "run_v7_log.jsonl"
HEAVYWEIGHT_CHAPTERS = {7}                     # 1-5 内无重场戏（保留机制开关）

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
        length_cap_chars=LENGTH_CAP_CHARS,
        # ADR-020 四件套：默认全开（defer_title/cast_injection/character_direction/perspective_memory）
    )
    if not res.ok:
        return {"ch": ch, "ok": False, "error": res.result[:200], "secs": round(time.time() - t0)}

    rec = {
        "ch": ch, "ok": True, "secs": round(time.time() - t0),
        "heavyweight": heavyweight,
        "chars": res.completeness.get("chars"),
        "ends_properly": res.completeness.get("ends_properly"),
        "length_truncated": res.length_truncated,
        "meta": res.completeness.get("meta_narration") or [],
        "dup_paragraphs": res.completeness.get("dup_paragraphs"),
        "dup_sentences": res.completeness.get("dup_sentences"),
        "bible": res.bible_injected, "attempts": res.attempts,
        "events": res.events_committed,
        # ---- ADR-020 观测 ----
        "chapter_title": res.chapter_title,
        "directions_built": res.directions_built,
        "perspectives_written": res.perspectives_written,
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
        "settings_added": getattr(res, "settings_pending", 0),
        "usage": getattr(res, "usage", None),
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
    ap.add_argument("--chapters", type=int, default=5)
    args = ap.parse_args()

    provider = LMStudioProvider(base_url=CLOUD_BASE, model=CLOUD_MODEL,
                                reasoning_effort="none", timeout_s=600)
    embedding = _make_embedding()
    tally = {"events": 0, "blocks": 0, "revised": 0, "truncated": 0, "polished": 0,
             "threads": 0, "directions": 0, "perspectives": 0}

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
        tally["directions"] += rec.get("directions_built", 0)
        tally["perspectives"] += rec.get("perspectives_written", 0)
        st = rec.get("state_updates") or {}
        print(f"[ch{ch}] {rec['secs']}s chars={rec['chars']} 完整={rec['ends_properly']}"
              f" 拟题=「{rec.get('chapter_title') or '-'}」"
              + (" 截断!" if rec.get("length_truncated") else "")
              + (f" 元叙事!{rec['meta']}" if rec.get("meta") else "")
              + (f" 重复段{rec.get('dup_paragraphs')}" if rec.get("dup_paragraphs") else "")
              + f" | 编纂 {rec['chronicle_written']} 条"
              f" 状态 {len(st)} 人 伏笔流转 {rec.get('threads_activated', 0)}"
              + (f" 冲突{len(rec['chronicle_conflicts'])}" if rec.get("chronicle_conflicts") else "")
              + f" | ADR20 调度{rec.get('directions_built', 0)} 视角{rec.get('perspectives_written', 0)}"
              + f" | 审校 block={rec.get('review_blocks', 0)}/{rec.get('review_blocks_chapter', 0)}"
              + f" 修订 {rec.get('events_revised', 0)} lessons+{rec.get('lessons_added', 0)}"
              + f" | AI味 {rec.get('ai_before')}→{rec.get('ai_after')}", flush=True)

    print(f"\nTOTAL {tally}", flush=True)


if __name__ == "__main__":
    main()
