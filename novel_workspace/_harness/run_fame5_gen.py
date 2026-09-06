"""云端 C 服务器 10 章生成 · proj-fame5-*（3080Ti FreeToken Qwen3.6-35B-A3B-FP8）。

复用 run_cloudc5.py 实证配置，差异：
- 章表 [(1,1)..(1,8), (2,1), (2,2)] 跨卷（卷 1 末章自动触发卷末审计 + 卷 2 承接）
- 细纲 1-1..1-8 由 build 产出；2-1/2-2 由 run_fame5_seed.py 阶段 1e 补齐
- 7 项线索验收素材随 produce_chapter 自动落账（lines_present 事件线卡 / 回写 /
  closing_candidate / 检查点 due / 卷末审计报告）

用法：python run_fame5_gen.py <project_id>   （缺省取最新 proj-fame5-*）
产出：drafts/chapters/{vol}-{ch}.md + reports/stats/generation-*.md + fame5_log.jsonl
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

os_env = __import__("os").environ
os_env.setdefault("NOVELIST_REVIEWER_TOKENS", "4096")

from novelist.consistency.reviewer import Reviewer  # noqa: E402
from novelist.core.embedding import LocalEmbedding, make_embedding  # noqa: E402
from novelist.core.memory import (MemoryIndex, MemoryQuery,  # noqa: E402
                                  MemoryRetriever, rollback_chapter)
from novelist.core.orchestrator import produce_chapter  # noqa: E402
from novelist.core.session import SessionInfo  # noqa: E402
from novelist.providers.deepseek import DeepSeekProvider  # noqa: E402
from novelist.providers.lmstudio import LMStudioProvider  # noqa: E402
from novelist.storage.workspace import Workspace  # noqa: E402

CHAPTERS = [(1, ch) for ch in range(1, 9)] + [(2, 1), (2, 2)]
LOG = Path(__file__).resolve().parent / "fame5_log.jsonl"
MODEL_C = "qwen3.6-35b-a3b-fp8"

# 后端选择：第 2 参数或 FAME5_BACKEND=deepseek|cloud（缺省 cloud）
BACKEND = (sys.argv[2] if len(sys.argv) > 2
           else os_env.get("FAME5_BACKEND", "cloud")).lower()
GEN_TOKENS = 6000 if BACKEND == "cloud" else 6000   # deepseek-chat 非思考，上限 8192
BROADCAST_CASTING = True


def resolve_pid() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    ws_root = ROOT / "novel_workspace"
    cands = sorted(p.name for p in ws_root.glob("proj-fame5-*") if p.is_dir())
    if not cands:
        raise SystemExit("未找到 proj-fame5-* 项目——先跑 run_fame5_seed.py")
    return cands[-1]


def make_provider():
    if BACKEND == "deepseek":
        key_file = Path(__file__).resolve().parent / ".deepseek_key"
        if key_file.exists():
            os_env.setdefault("DeepSeek-API-KEY", key_file.read_text(encoding="utf-8").strip())
        p = DeepSeekProvider(model="deepseek-chat", timeout_s=600.0)
        print(f"backend=deepseek model={p.model}", flush=True)
        return p
    return LMStudioProvider(
        base_url="http://127.0.0.1:18006",
        model=MODEL_C,
        enable_thinking=False,
        reasoning_aware=True,
        timeout_s=1800,
    )


def make_embedding():
    try:
        emb = LocalEmbedding()
        emb.embed(["预热闹活"])  # 首调触发 ONNX 加载
        return emb
    except Exception as e:  # noqa: BLE001
        print(f"内置 LocalEmbedding 不可用（{type(e).__name__}）→ 关键词降级", flush=True)
    return make_embedding("keyword-fallback")


def recall(ws, pid: str, vol: int, ch: int, embedding, top_k: int = 5) -> list[str]:
    gist = ws.outline_chapter_path(pid, vol, ch)
    gist_text = gist.read_text(encoding="utf-8")[:800] if gist.exists() else ""
    idx = MemoryIndex.load(ws, pid)
    if not idx.fragments or idx.kind != getattr(embedding, "kind", "keyword-hash"):
        n = idx.rebuild(ws, pid, embedding)
        print(f"[recall] 索引重建：kind={idx.kind} dim={idx.dim} fragments={n}", flush=True)
    if not idx.fragments:
        return []
    hits = MemoryRetriever(idx, embedding=embedding).query(
        MemoryQuery(query=gist_text or f"第 {vol} 卷第 {ch} 章", top_k=top_k))
    return [f"- [{h.kind} @ {h.source.get('vol')}:{h.source.get('ch')}] {h.text}"
            for h in hits if h.score > 0]


def run_chapter(pid: str, vol: int, ch: int, provider, embedding) -> dict:
    t0 = time.time()
    ws = Workspace(root=str(ROOT / "novel_workspace"))
    sess = SessionInfo(project_id=pid, agent="orchestrator")
    rolled = rollback_chapter(ws, pid, vol, ch, embedding=embedding)
    memories = recall(ws, pid, vol, ch, embedding)

    gist = ws.outline_chapter_path(pid, vol, ch)
    gist_text = gist.read_text(encoding="utf-8") if gist.exists() else ""

    res = produce_chapter(
        ws, pid, vol, ch, provider, session=sess,
        prefer_direct=True, generation_tokens=GEN_TOKENS, embedding=embedding,
        inject_bible=True, memories=memories or None,
        event_loop=True, screenplay=False,
        polish=True, max_retries=1, commit_chapter_event=None,
        knowledge_llm=True, event_review=True,
        event_polish=True, readback=True,
        jit_characters=True, supplement_settings=True,
        broadcast_casting=BROADCAST_CASTING,
        seam_review=True, volume_facts=True,
    )
    if not res.ok:
        return {"vol": vol, "ch": ch, "ok": False,
                "error": res.result[:300], "secs": round(time.time() - t0)}

    rec = {
        "vol": vol, "ch": ch, "ok": True, "secs": round(time.time() - t0),
        "chars": res.completeness.get("chars"),
        "ends_properly": res.completeness.get("ends_properly"),
        "meta": res.completeness.get("meta_narration") or [],
        "dup_paragraphs": res.completeness.get("dup_paragraphs"),
        "dup_sentences": res.completeness.get("dup_sentences"),
        "bible": res.bible_injected, "attempts": res.attempts,
        "first_seen_patched": getattr(res, "first_seen_patched", 0),
        "seam_hits": getattr(res, "seam_hits", 0) if hasattr(res, "seam_hits") else 0,
        "volume_facts_built": getattr(res, "volume_facts_built", False),
        "events": res.events_committed,
        "chapter_title": res.chapter_title,
        "directions_built": res.directions_built,
        "perspectives_written": res.perspectives_written,
        "broadcasts": getattr(res, "broadcasts_built", 0),
        "chronicle_written": getattr(res.chronicle, "written", 0),
        "chronicle_conflicts": getattr(res.chronicle, "conflicts", []),
        "threads_activated": getattr(res.chronicle, "threads_activated", 0),
        "state_updates": getattr(res.chronicle, "state_updates", {}),
        "lines_report": getattr(res, "lines_report", None),
        "checkpoint_ran": getattr(res, "checkpoint_ran", False),
        "volume_audit_ran": getattr(res, "volume_audit_ran", False),
        "polish_applied": bool(res.polish and res.polish.changed),
        "ai_before": getattr(res.polish, "before", None) and res.polish.before.score,
        "ai_after": getattr(res.polish, "after", None) and res.polish.after.score,
        "review_blocks": res.review_blocks,
        "events_revised": res.events_revised,
        "lessons_added": res.lessons_added,
        "jit_added": res.jit_added,
        "settings_pending": res.settings_pending,
        "rel_pairs": res.rel_pairs,
        "rel_proposals": res.rel_proposals,
    }

    draft = ws.draft_path(pid, vol, ch)
    reviewer = Reviewer(ws, pid, provider)
    if draft.exists():
        try:
            issues = reviewer.review(draft.read_text(encoding="utf-8"), vol, ch,
                                     gist_text=gist_text, memories=memories)
            rec["review"] = [{"level": i.level, "category": i.category, "detail": i.detail}
                             for i in issues]
            rec["review_blocks_chapter"] = sum(1 for i in issues if i.level == "block")
        except Exception as e:  # noqa: BLE001
            rec["review_error"] = f"{type(e).__name__}: {e}"[:200]
            print(f"[{vol}-{ch}] 审校异常（不影响正文）: {rec['review_error']}", flush=True)
    return rec


def main() -> None:
    pid = resolve_pid()
    provider = make_provider()
    embedding = make_embedding()
    print(f"== 10 章生成：{pid}（backend={BACKEND} gen={GEN_TOKENS}）==", flush=True)
    print(f"embedding mode: {embedding.kind}", flush=True)
    all_ok = True
    for vol, ch in CHAPTERS:
        print(f"[{vol}-{ch}] start {time.strftime('%H:%M:%S')}", flush=True)
        try:
            rec = run_chapter(pid, vol, ch, provider, embedding)
        except Exception as e:  # noqa: BLE001
            rec = {"vol": vol, "ch": ch, "ok": False,
                   "error": f"{type(e).__name__}: {e}"[:300], "secs": 0}
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"pid": pid, **rec}, ensure_ascii=False) + "\n")
        if not rec.get("ok"):
            all_ok = False
            print(f"[{vol}-{ch}] FAILED {rec.get('error')}", flush=True)
            continue
        st = rec.get("state_updates") or {}
        print(f"[{vol}-{ch}] {rec['secs']}s chars={rec['chars']} 完整={rec['ends_properly']}"
              f" 拟题=「{rec.get('chapter_title') or '-'}」"
              + (f" 元叙事!{rec.get('meta')}" if rec.get("meta") else "")
              + (f" 重复段{rec.get('dup_paragraphs')}" if rec.get("dup_paragraphs") else "")
              + f" | 事件{rec.get('events')} 编纂{rec.get('chronicle_written')} 状态{len(st)}人"
              + (f" 冲突{len(rec['chronicle_conflicts'])}" if rec.get("chronicle_conflicts") else "")
              + f" | 线索 {json.dumps(rec.get('lines_report') or {}, ensure_ascii=False)[:160]}"
              + (f" 检查点✓" if rec.get("checkpoint_ran") else "")
              + (f" 卷末审计✓" if rec.get("volume_audit_ran") else "")
              + f" | ADR20 调度{rec.get('directions_built', 0)}"
              f" 视角{rec.get('perspectives_written', 0)} 广播{rec.get('broadcasts', 0)}"
              + f" | 审校 block={rec.get('review_blocks', 0)}"
              f" 修订{rec.get('events_revised', 0)}"
              + f" | AI味 {rec.get('ai_before')}→{rec.get('ai_after')}"
              + f" | 账本 {rec.get('rel_pairs', 0)}对/提案{rec.get('rel_proposals', 0)}", flush=True)
        time.sleep(1.5)
    print(f"ALL {'OK' if all_ok else 'PARTIAL'} → {LOG}", flush=True)


if __name__ == "__main__":
    main()
