"""《断玉青冥》v2：全部走系统自身能力的新链路。

与 v1（`run_novel.py`）的关键差别：**本脚本几乎不代做系统的事**。

| 环节 | v1 | v2 |
| --- | --- | --- |
| 圣经注入 | 脚本代拼 system_prompt | `core/context.py`（系统） |
| 完整性校验与重试 | 无（人工发现截断后重跑） | `produce_chapter(validate=True, max_retries=1)` |
| 事件抽取 | 脚本代行编纂员 | `core/chronicler.py`（系统） |
| 记忆回退 | 脚本直接改 JSON | `core/memory.rollback_chapter`（系统） |
| 文风优化 | 无 | `core/polish.polish_chapter`（系统） |
| 语义审校 | 无 | `consistency/reviewer.py`（系统） |

每章流程：先忆 → 生成（含完整性校验/重试）→ 文风润色 → 编纂员回写事件 → 审校师复核
→ 有 block 级问题则带审校意见重生成一次。

用法：python novel_workspace/_harness/run_novel_v2.py [--from-ch N] [--chapters N]
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
from novelist.core.chronicler import Chronicler  # noqa: E402
from novelist.core.embedding import make_embedding  # noqa: E402
from novelist.core.memory import MemoryIndex, MemoryQuery, MemoryRetriever, rollback_chapter  # noqa: E402
from novelist.core.orchestrator import produce_chapter  # noqa: E402
from novelist.core.session import SessionInfo  # noqa: E402
from novelist.providers.lmstudio import LMStudioProvider  # noqa: E402
from novelist.storage.workspace import Workspace  # noqa: E402

PID = "proj-duanyu"
VOL = 1
MODEL = "qwen/qwen3.5-9b"
# 预算：qwen3.5 是思考型模型，思考 token 计入 max_tokens（人工审查第二批第 1 条）。
# 基础预算给 1000，由 LMStudioProvider(reasoning_aware=True) 在正文被思考挤没时
# 自动加预算重试一次。不再往上堆——1600 以上本机实测 ErrorOutOfDeviceMemory。
GEN_TOKENS = 1200
BUDGET_RETRIES = 1
# 完整性重试：截断的章节必须重生成（带"必须完整收束"的修复提示）。
# 之前为了躲显存把它设成 0，导致第 2/4 章带着截断就过了——负载不是瓶颈时不该省这个。
COMPLETENESS_RETRIES = 1
COOLDOWN_S = 0  # 无需为显存让路

ws = Workspace(root=str(ROOT / "novel_workspace"))
LOG = Path(__file__).resolve().parent / "run_v2_log.jsonl"


def recall(vol: int, ch: int, embedding, top_k: int = 6) -> list[str]:
    """先忆：系统自身的记忆检索。"""
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


def run_chapter(ch: int, provider, embedding, *, do_polish: bool = True) -> dict:
    t0 = time.time()
    sess = SessionInfo(project_id=PID, agent="orchestrator")

    # 1) 回退本章旧记忆（系统能力，B-07）
    rolled = rollback_chapter(ws, PID, VOL, ch)

    # 2) 先忆
    memories = recall(VOL, ch, embedding)

    # 3) 生成（圣经注入 + 完整性校验/重试 + 文风润色 + 编纂员回写 全由系统完成）
    res = produce_chapter(
        ws, PID, VOL, ch, provider, session=sess,
        prefer_direct=True, generation_tokens=GEN_TOKENS, embedding=embedding,
        inject_bible=True, memories=memories or None, polish=do_polish,
        # 预算不足由 provider 的 reasoning_aware 处理；这里是**完整性**重试——
        # 截断的章节必须重生成，否则会带着半句话进发布包。
        max_retries=COMPLETENESS_RETRIES,
    )
    if not res.ok:
        return {"ch": ch, "ok": False, "error": res.result[:200], "secs": round(time.time() - t0)}

    rec = {
        "ch": ch,
        "ok": True,
        "secs": round(time.time() - t0),
        "bible": res.bible_injected,
        "attempts": res.attempts,
        "chars": res.completeness.get("chars"),
        "ends_properly": res.completeness.get("ends_properly"),
        "meta": res.completeness.get("meta_narration") or [],
        "rolled_back": rolled,
        "events": res.events_committed,
        "chronicle_written": getattr(res.chronicle, "written", 0),
        "chronicle_conflicts": getattr(res.chronicle, "conflicts", []),
        "polish_applied": bool(res.polish and res.polish.changed),
        "ai_before": getattr(res.polish, "before", None) and res.polish.before.score,
        "ai_after": getattr(res.polish, "after", None) and res.polish.after.score,
    }

    # 4) 审校师复核（系统能力，B-08）
    reviewer = Reviewer(ws, PID, provider)
    draft = ws.draft_path(PID, VOL, ch)
    gist = ws.outline_chapter_path(PID, VOL, ch)
    issues = reviewer.review(
        draft.read_text(encoding="utf-8") if draft.exists() else "",
        VOL, ch,
        gist_text=gist.read_text(encoding="utf-8") if gist.exists() else "",
        memories=memories,
    ) if draft.exists() else []
    rec["review"] = [{"level": i.level, "category": i.category, "detail": i.detail} for i in issues]
    rec["review_blocks"] = sum(1 for i in issues if i.level == "block")

    # 5) 有 block 级问题 → 带审校意见重生成一次
    if rec["review_blocks"] and do_polish:
        fix_hint = ["【审校意见·必须修正】"] + [
            f"- {i.category}：{i.detail}" + (f"（建议：{i.suggestion}）" if i.suggestion else "")
            for i in issues if i.level == "block"
        ]
        res2 = produce_chapter(
            ws, PID, VOL, ch, provider, session=sess,
            prefer_direct=True, generation_tokens=GEN_TOKENS, embedding=embedding,
            inject_bible=True, memories=(memories or []) + fix_hint, polish=True, max_retries=1,
        )
        rec["revised"] = bool(res2.ok)
        rec["revised_blocks"] = rec["review_blocks"]
        if res2.ok:
            rec["chars"] = res2.completeness.get("chars", rec["chars"])
            rec["ai_after"] = getattr(res2.polish, "after", None) and res2.polish.after.score
            rec["events"] = res2.events_committed
            # 重生成后补齐编纂/润色字段——否则日志里会出现"编纂 0 但记忆里有事件"的假象
            rec["chronicle_written"] = getattr(res2.chronicle, "written", 0)
            rec["chronicle_conflicts"] = getattr(res2.chronicle, "conflicts", [])
            rec["polish_applied"] = bool(res2.polish and res2.polish.changed)
            if getattr(res2.polish, "before", None) is not None:
                rec["ai_before"] = res2.polish.before.score
            issues2 = reviewer.review(
                ws.draft_path(PID, VOL, ch).read_text(encoding="utf-8"), VOL, ch,
                gist_text=gist.read_text(encoding="utf-8") if gist.exists() else "")
            rec["review_blocks"] = sum(1 for i in issues2 if i.level == "block")
    return rec


LOCK = Path(__file__).resolve().parent / ".v2.lock"


def _acquire_lock() -> None:
    """单实例锁——防止两个进程同时跑同一批文件。

    踩过的坑：后台任务用 `nohup &` 起时父 shell 退出后子进程可能存活，
    再起一个就会并发写同一批文件，导致记忆层事件数翻倍、日志交叉。
    """
    if LOCK.exists():
        raw = LOCK.read_text(encoding="utf-8").strip()
        if raw and raw.isdigit() and _pid_alive(int(raw)):
            raise SystemExit(f"another run is active (pid={raw}); "
                             f"wait for it, or `python novel_workspace/_harness/reset.py` to clear")
    LOCK.write_text(str(os.getpid()), encoding="utf-8")


def _pid_alive(pid: int) -> bool:
    import subprocess

    try:
        out = subprocess.run(["ps", "-W"], capture_output=True, text=True, timeout=10).stdout
        return any(str(pid) in ln for ln in out.splitlines())
    except Exception:  # noqa: BLE001
        return False  # 查不到就当作已死，允许启动


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-ch", type=int, default=1)
    ap.add_argument("--chapters", type=int, default=10)
    ap.add_argument("--no-polish", action="store_true")
    args = ap.parse_args()

    _acquire_lock()
    provider = LMStudioProvider(model=MODEL, timeout_s=900,
                                reasoning_aware=True, budget_retries=BUDGET_RETRIES)
    embedding = make_embedding("keyword-fallback")
    tally = {"events": 0, "blocks": 0, "truncated": 0, "polished": 0}

    for ch in range(args.from_ch, args.from_ch + args.chapters):
        if ch > args.from_ch:
            time.sleep(COOLDOWN_S)
        rec = run_chapter(ch, provider, embedding, do_polish=not args.no_polish)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if not rec.get("ok"):
            print(f"[ch{ch}] FAILED {rec.get('error')}", flush=True)
            continue
        tally["events"] += rec.get("events", 0)
        tally["blocks"] += rec.get("review_blocks", 0)
        tally["truncated"] += 0 if rec.get("ends_properly") else 1
        tally["polished"] += 1 if rec.get("polish_applied") else 0
        print(f"[ch{ch}] {rec['secs']}s chars={rec['chars']} 完整={rec['ends_properly']} "
              f"元叙事={rec['meta'] or '无'} | 编纂 {rec['chronicle_written']} 条"
              + (f" 冲突{len(rec['chronicle_conflicts'])}" if rec["chronicle_conflicts"] else "")
              + f" | AI味 {rec['ai_before']}→{rec['ai_after']}"
              + f" | 审校 block={rec['review_blocks']}"
              + (" (已重生成)" if rec.get("revised") else ""), flush=True)

    print(f"\nTOTAL {tally}", flush=True)


if __name__ == "__main__":
    main()
