"""新书 5 章测试·proj-20260903194907（DeepSeek / 通用包 / 都市修仙回归流）。

复用 run_ch6_deepseek.py 已实证配置（ADR-020 四件套默认全开）。
目的：模型能力足够时暴露系统设计缺陷 + 实测成本。
产出：drafts/chapters/1-{1..5}.md + reports/stats/generation-*.md（成本审计自动落）+ 本文件 log jsonl。
用法：python run_firstbook5.py  （env DeepSeek-API-KEY 必需）
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

os.environ.setdefault("NOVELIST_REVIEWER_TOKENS", "4096")

from novelist.consistency.reviewer import Reviewer  # noqa: E402
from novelist.core.embedding import OpenAIEmbedding, make_embedding  # noqa: E402
from novelist.core.memory import (MemoryIndex, MemoryQuery,  # noqa: E402
                                  MemoryRetriever, rollback_chapter)
from novelist.core.orchestrator import produce_chapter  # noqa: E402
from novelist.core.session import SessionInfo  # noqa: E402
from novelist.providers.deepseek import DeepSeekProvider  # noqa: E402
from novelist.storage.workspace import Workspace  # noqa: E402

PID = "proj-20260903194907"
VOL = 1
CHAPTERS = [1, 2, 3, 4, 5]
GEN_TOKENS = 4096       # deepseek-chat 非思考，预算即正文预算
BROADCAST_CASTING = True   # ADR-021 事件级选角 LLM 推理（v1 对照验证开；失败自动降级不阻断）
LOG = Path(__file__).resolve().parent / "firstbook5_log_v2.jsonl"  # v2：B/A/C/D 后真机回归，与 9/3 基线分档


def _make_embedding():
    """内置 LocalEmbedding（fastembed/ONNX nomic，CPU 离线）优先——统一全系统 embedding 任务；
    不可用时退 LM-Studio nomic（同款模型同语义），最后关键词降级。"""
    from novelist.core.embedding import LocalEmbedding, make_embedding

    try:
        emb = LocalEmbedding()
        emb.embed(["预热闹活"])  # 首次调用触发 ONNX 权重加载（已缓存则秒回）
        return emb
    except Exception as e:  # noqa: BLE001
        print(f"内置 LocalEmbedding 不可用（{type(e).__name__}），退 LM-Studio 探测", flush=True)
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


def recall(ws, ch: int, embedding, top_k: int = 5) -> list[str]:
    gist = ws.outline_chapter_path(PID, VOL, ch)
    gist_text = gist.read_text(encoding="utf-8")[:800] if gist.exists() else ""
    idx = MemoryIndex.load(ws, PID)
    # kind/dim 不匹配 → 旧向量与当前 embedding 失配（cosine 维度不等恒 0，检索会静默全灭），
    # 必须用当前 embedding 全量重建，不能只看 fragments 是否为空。
    if not idx.fragments or idx.kind != getattr(embedding, "kind", "keyword-hash"):
        n = idx.rebuild(ws, PID, embedding)
        print(f"[recall] 索引重建：kind={idx.kind} dim={idx.dim} fragments={n}", flush=True)
    if not idx.fragments:
        return []
    hits = MemoryRetriever(idx, embedding=embedding).query(
        MemoryQuery(query=gist_text or f"第 {VOL} 卷第 {ch} 章", top_k=top_k))
    return [f"- [{h.kind} @ {h.source.get('vol')}:{h.source.get('ch')}] {h.text}" for h in hits if h.score > 0]


def run_chapter(ch: int, provider, embedding) -> dict:
    t0 = time.time()
    ws = Workspace(root=str(ROOT / "novel_workspace"))
    sess = SessionInfo(project_id=PID, agent="orchestrator")
    rolled = rollback_chapter(ws, PID, VOL, ch)
    memories = recall(ws, ch, embedding)

    gist = ws.outline_chapter_path(PID, VOL, ch)
    gist_text = gist.read_text(encoding="utf-8") if gist.exists() else ""

    res = produce_chapter(
        ws, PID, VOL, ch, provider, session=sess,
        prefer_direct=True, generation_tokens=GEN_TOKENS, embedding=embedding,
        inject_bible=True, memories=memories or None,
        event_loop=True, screenplay=False,
        polish=True, max_retries=1, commit_chapter_event=None,
        knowledge_llm=True, event_review=True,
        event_polish=True, readback=True,
        jit_characters=True, supplement_settings=True,
        broadcast_casting=BROADCAST_CASTING,
    )
    if not res.ok:
        return {"ch": ch, "ok": False, "error": res.result[:300], "secs": round(time.time() - t0)}

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
        "broadcasts": getattr(res, "broadcasts_built", 0),
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

    draft = ws.draft_path(PID, VOL, ch)
    reviewer = Reviewer(ws, PID, provider)
    if draft.exists():
        try:
            issues = reviewer.review(draft.read_text(encoding="utf-8"), VOL, ch,
                                     gist_text=gist_text, memories=memories)
            rec["review"] = [{"level": i.level, "category": i.category, "detail": i.detail} for i in issues]
            rec["review_blocks_chapter"] = sum(1 for i in issues if i.level == "block")
        except Exception as e:  # noqa: BLE001
            rec["review_error"] = f"{type(e).__name__}: {e}"[:200]
            print(f"[ch{ch}] 审校异常（不影响正文）: {rec['review_error']}", flush=True)
    return rec


def main() -> None:
    provider = DeepSeekProvider(timeout_s=180)
    embedding = _make_embedding()
    print(f"embedding mode: {embedding.kind}", flush=True)
    all_ok = True
    for ch in CHAPTERS:
        t = time.time()
        print(f"[ch{ch}] start {time.strftime('%H:%M:%S')}", flush=True)
        try:
            rec = run_chapter(ch, provider, embedding)
        except Exception as e:  # noqa: BLE001
            rec = {"ch": ch, "ok": False, "error": f"{type(e).__name__}: {e}"[:300], "secs": 0}
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if not rec.get("ok"):
            all_ok = False
            print(f"[ch{ch}] FAILED {rec.get('error')}", flush=True)
            continue
        st = rec.get("state_updates") or {}
        print(f"[ch{ch}] {rec['secs']}s chars={rec['chars']} 完整={rec['ends_properly']}"
              f" 拟题=「{rec.get('chapter_title') or '-'}」"
              + (" 截断!" if rec.get("length_truncated") else "")
              + (f" 元叙事!{rec.get('meta')}" if rec.get("meta") else "")
              + (f" 重复段{rec.get('dup_paragraphs')}" if rec.get("dup_paragraphs") else "")
              + f" | 事件{rec.get('events')} 编纂{rec.get('chronicle_written')} 状态{len(st)}人"
              + (f" 冲突{len(rec['chronicle_conflicts'])}" if rec.get("chronicle_conflicts") else "")
              + f" | ADR20 调度{rec.get('directions_built', 0)} 视角{rec.get('perspectives_written', 0)}"
              + f" 广播{rec.get('broadcasts', 0)}"
              + f" | 审校 block={rec.get('review_blocks', 0)}/{rec.get('review_blocks_chapter', 0)}"
              + f" 修订{rec.get('events_revised', 0)}"
              + f" | AI味 {rec.get('ai_before')}→{rec.get('ai_after')}"
              + f" | 账本 {rec.get('rel_pairs', 0)}对/提案{rec.get('rel_proposals', 0)}", flush=True)
        # 章间留 1.5s，避免限流粘连
        time.sleep(1.5)
    print(f"ALL {'OK' if all_ok else 'PARTIAL'} → {LOG}", flush=True)


if __name__ == "__main__":
    main()
