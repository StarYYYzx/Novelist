"""《五五开》v8 本地验证：proj-yelan3 · LM-Studio qwen3.5-9b 跑单章。

与 v7（服务器 C / FreeToken 35B）同一 produce_chapter 配置，仅换后端：
- 本地 127.0.0.1:1234，model=qwen/qwen3.5-9b（LM-Studio 思考关不掉 → reasoning_aware=True 兜底）
- GEN_TOKENS=1400（经验界：system prompt 上千字后 >1400 易空输出，>~1600 易 OOM）
- 用途：真机验证 D12（跨事件防重复登场）/ D14（hidden_level 注入）/ D15（首章开篇契约）

用法：python novel_workspace/_harness/run_novel_v8_local.py [--ch 1]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import os  # noqa: E402

from novelist.consistency.reviewer import Reviewer  # noqa: E402,F401
from novelist.core.embedding import OpenAIEmbedding, make_embedding  # noqa: E402
from novelist.core.memory import MemoryIndex, MemoryQuery, MemoryRetriever, rollback_chapter  # noqa: E402
from novelist.core.orchestrator import produce_chapter  # noqa: E402
from novelist.core.session import SessionInfo  # noqa: E402
from novelist.providers.lmstudio import LMStudioProvider  # noqa: E402
from novelist.storage.workspace import Workspace  # noqa: E402

PID = "proj-yelan3"
LOCAL_BASE = "http://127.0.0.1:1234"   # LM-Studio
LOCAL_MODEL = "qwen/qwen3.5-9b"
GEN_TOKENS = 1400                       # 思考吃预算 + OOM 两头夹击的经验值
LENGTH_CAP_CHARS = 7000
LOG = Path(__file__).resolve().parent / "run_v8_local_log.jsonl"
HEAVYWEIGHT_CHAPTERS: set[int] = set()

ws = Workspace(root=str(ROOT / "novel_workspace"))


def _make_embedding():
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:1234/v1/models", timeout=3) as r:
            if r.status == 200:
                return OpenAIEmbedding(
                    model="text-embedding-nomic-embed-text-v1.5",
                    base_url="http://127.0.0.1:1234/v1", api_key="lmstudio")
    except Exception:  # noqa: BLE001
        pass
    print("本地 embedding 不可达 → 关键词检索模式", flush=True)
    return make_embedding("keyword-fallback")


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
    if rolled:
        print(f"[ch{ch}] 已回滚旧记忆 {rolled} 条", flush=True)
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
    )
    if not res.ok:
        return {"ch": ch, "ok": False, "error": str(res.result)[:200], "secs": round(time.time() - t0)}

    rec = {
        "ch": ch, "ok": True, "secs": round(time.time() - t0),
        "chars": res.completeness.get("chars"),
        "ends_properly": res.completeness.get("ends_properly"),
        "length_truncated": res.length_truncated,
        "meta": res.completeness.get("meta_narration") or [],
        "dup_paragraphs": res.completeness.get("dup_paragraphs"),
        "dup_sentences": res.completeness.get("dup_sentences"),
        "bible": res.bible_injected, "attempts": res.attempts,
        "events": res.events_committed,
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
        "settings_added": res.settings_added,
    }
    return rec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ch", type=int, default=1)
    args = ap.parse_args()

    provider = LMStudioProvider(base_url=LOCAL_BASE, model=LOCAL_MODEL,
                                reasoning_aware=True, timeout_s=420)
    embedding = _make_embedding()
    print(f"[v8-local] PID={PID} ch={args.ch} model={LOCAL_MODEL} gen_tokens={GEN_TOKENS}", flush=True)

    t0 = time.time()
    rec = run_chapter(args.ch, provider, embedding)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    if not rec.get("ok"):
        print(f"[ch{args.ch}] FAILED {rec.get('error')}", flush=True)
        return
    st = rec.get("state_updates") or {}
    print(f"[ch{args.ch}] {rec['secs']}s chars={rec['chars']} 完整={rec['ends_properly']}"
          f" 拟题=「{rec.get('chapter_title') or '-'}」"
          + (" 截断!" if rec.get("length_truncated") else "")
          + (f" 元叙事!{rec['meta']}" if rec.get("meta") else "")
          + (f" 重复段{rec.get('dup_paragraphs')}" if rec.get("dup_paragraphs") else "")
          + f" | 编纂 {rec['chronicle_written']} 条 状态 {len(st)} 人 伏笔 {rec.get('threads_activated', 0)}"
          + f" | ADR20 调度{rec.get('directions_built', 0)} 视角{rec.get('perspectives_written', 0)}"
          + f" | 审校 block={rec.get('review_blocks', 0)} 修订 {rec.get('events_revised', 0)}"
          + f" | AI味 {rec.get('ai_before')}→{rec.get('ai_after')}", flush=True)
    print(f"TOTAL {round(time.time() - t0)}s", flush=True)


if __name__ == "__main__":
    main()
