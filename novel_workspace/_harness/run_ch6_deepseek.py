"""M3r 验收·ch6 DeepSeek 单章对照（提交 b2a2171 注入面收口后）。

目的：v7（qwen3.6-35b，ch1-5，人物卡无补喂数据）之后，用 DeepSeek 续跑 ch6，
验证补喂数据（relationships 完整句 + behavior_rules）真实进入生成上下文并被模型使用。
注意混杂变量：模型（qwen3.6→deepseek-chat）与数据（无料→补喂）同时变了——
本跑定位是「注入面冒烟 + DeepSeek 质量预检」，严格 A/B 需同模型再跑。

后端：DeepSeek API（api.deepseek.com，key=DeepSeek-API-KEY，deepseek-chat 非思考）。
配置与 v7 对齐：ADR-020 四件套全开 / 事件循环 / 审校 / 润色 / supplement_settings。
产出：drafts/chapters/1-6.md（真实推进 yelan3 到 ch6）+ 本脚本同目录 run_ch6_ds_log.jsonl

用法：python run_ch6_deepseek.py
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
from novelist.core.memory import MemoryIndex, MemoryQuery, MemoryRetriever, rollback_chapter  # noqa: E402
from novelist.core.orchestrator import produce_chapter  # noqa: E402
from novelist.core.session import SessionInfo  # noqa: E402
from novelist.providers.deepseek import DeepSeekProvider  # noqa: E402
from novelist.storage.workspace import Workspace  # noqa: E402

PID = "proj-yelan3"
GEN_TOKENS = 4096       # deepseek-chat 非思考，预算即正文预算（v7 的 1200 是 qwen3.6 吞吐妥协）
LENGTH_CAP_CHARS = 7000
LOG = Path(__file__).resolve().parent / "run_ch6_ds_log.jsonl"


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


def recall(ws, ch: int, embedding, top_k: int = 5) -> list[str]:
    gist = ws.outline_chapter_path(PID, 1, ch)
    gist_text = gist.read_text(encoding="utf-8")[:800] if gist.exists() else ""
    idx = MemoryIndex.load(ws, PID)
    if not idx.fragments:
        idx.rebuild(ws, PID, embedding)
    if not idx.fragments:
        return []
    hits = MemoryRetriever(idx, embedding=embedding).query(
        MemoryQuery(query=gist_text or f"第 1 卷第 {ch} 章", top_k=top_k))
    return [f"- [{h.kind} @ {h.source.get('vol')}:{h.source.get('ch')}] {h.text}" for h in hits if h.score > 0]


def run_chapter(ch: int, provider, embedding) -> dict:
    t0 = time.time()
    ws = Workspace(root=str(ROOT / "novel_workspace"))
    sess = SessionInfo(project_id=PID, agent="orchestrator")
    rolled = rollback_chapter(ws, PID, 1, ch)
    memories = recall(ws, ch, embedding)

    gist = ws.outline_chapter_path(PID, 1, ch)
    gist_text = gist.read_text(encoding="utf-8") if gist.exists() else ""

    res = produce_chapter(
        ws, PID, 1, ch, provider, session=sess,
        prefer_direct=True, generation_tokens=GEN_TOKENS, embedding=embedding,
        inject_bible=True, memories=memories or None,
        event_loop=True, screenplay=False,
        polish=True, max_retries=1, commit_chapter_event=None,
        knowledge_llm=True, event_review=True,
        event_polish=True, readback=True,
        jit_characters=True, supplement_settings=True,
        length_cap_chars=LENGTH_CAP_CHARS,
        # ADR-020 四件套默认全开（defer_title/cast_injection/character_direction/perspective_memory）
    )
    if not res.ok:
        return {"ch": ch, "ok": False, "error": res.result[:200], "secs": round(time.time() - t0)}

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
        "settings_pending": res.settings_pending,  # P0-B：改名（settings_added 已废弃）
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
            rec["review_error"] = f"{type(e).__name__}: {e}"[:200]
            print(f"[ch{ch}] 审校异常（不影响正文）: {rec['review_error']}", flush=True)
    return rec


def main() -> None:
    provider = DeepSeekProvider(timeout_s=180)
    embedding = _make_embedding()
    try:
        rec = run_chapter(6, provider, embedding)
    except Exception as e:  # noqa: BLE001 - 异常也必须落日志（ch6 首跑日志丢失的教训）
        rec = {"ch": 6, "ok": False, "error": f"{type(e).__name__}: {e}"[:300],
               "secs": None}
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    if not rec.get("ok"):
        print(f"[ch6] FAILED {rec.get('error')}", flush=True)
        return
    st = rec.get("state_updates") or {}
    print(f"[ch6] {rec['secs']}s chars={rec['chars']} 完整={rec['ends_properly']}"
          f" 拟题=「{rec.get('chapter_title') or '-'}」"
          + (" 截断!" if rec.get("length_truncated") else "")
          + (f" 元叙事!{rec['meta']}" if rec.get("meta") else "")
          + (f" 重复段{rec.get('dup_paragraphs')}" if rec.get("dup_paragraphs") else "")
          + f" | 编纂 {rec.get('chronicle_written')} 条 状态 {len(st)} 人"
          + (f" 冲突{len(rec['chronicle_conflicts'])}" if rec.get("chronicle_conflicts") else "")
          + f" | ADR20 调度{rec.get('directions_built', 0)} 视角{rec.get('perspectives_written', 0)}"
          + f" | 审校 block={rec.get('review_blocks', 0)}/{rec.get('review_blocks_chapter', 0)}"
          + f" 修订 {rec.get('events_revised', 0)}"
          + f" | AI味 {rec.get('ai_before')}→{rec.get('ai_after')}", flush=True)
    print(f"\n详情已写入 {LOG}", flush=True)


if __name__ == "__main__":
    main()
