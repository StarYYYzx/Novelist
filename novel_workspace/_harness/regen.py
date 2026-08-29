"""重生成被截断/有问题的章节，并回滚该章已写入的记忆。

⚠️ 触及一个系统缺口：**记忆层没有修订与回退能力**。docs/06 §4.4 声称冲突记忆"可回滚"，
但 `MemoryWriter` 只有 append_*，没有任何撤回/修订接口。章节重写后，旧事件仍留在
memory/ 里与新事件并存（重复且矛盾）。本脚本只能直接改文件来做"记忆回滚"。

用法：python novel_workspace/_harness/regen.py 2 9 10
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "novel_workspace" / "_harness"))

from novelist.core.embedding import make_embedding  # noqa: E402
from novelist.core.memory import (  # noqa: E402
    MemoryConflictError,
    MemoryIndex,
    MemoryQuery,
    MemoryRetriever,
    MemoryWriter,
)
from novelist.core.orchestrator import produce_chapter  # noqa: E402
from novelist.core.session import SessionInfo  # noqa: E402
from novelist.providers.lmstudio import LMStudioProvider  # noqa: E402
from novelist.storage.workspace import Workspace  # noqa: E402

import run_novel as H  # noqa: E402  复用其 prompt / 抽取逻辑

PID = "proj-duanyu"
VOL = 1
TOKENS = 1500  # CLI 硬编码 400 会截断；这里给足


def rollback_memory(ws: Workspace, ch: int) -> int:
    """回滚第 ch 章已写入的真实事件（合成章级事件保留，以示系统行为）。"""
    removed = 0
    pe_path = ws._abs(f"{PID}/memory/plot_events.json")
    events = json.loads(pe_path.read_text(encoding="utf-8")) if pe_path.exists() else []
    kept = []
    for e in events:
        at = e.get("at") or {}
        if at.get("vol") == VOL and at.get("ch") == ch and e.get("type") != "chapter":
            removed += 1
            continue
        kept.append(e)
    ws.write_json(pe_path, kept)

    hist_dir = ws._abs(f"{PID}/memory/character_histories")
    if hist_dir.is_dir():
        for f in hist_dir.glob("*.json"):
            data = json.loads(f.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            entries = [x for x in data.get("entries", [])
                       if (x.get("at") or {}).get("vol") != VOL or (x.get("at") or {}).get("ch") != ch]
            if len(entries) != len(data.get("entries", [])):
                data["entries"] = entries
                data["revision"] = int(data.get("revision", 0)) + 1
                ws.write_json(f, data)
    return removed


def main() -> None:
    targets = [int(x) for x in sys.argv[1:]] or [2, 9, 10]
    ws = Workspace(root=str(ROOT / "novel_workspace"))
    provider = LMStudioProvider(model=H.MODEL, timeout_s=900)
    embedding = make_embedding("keyword-fallback")
    sess = SessionInfo(project_id=PID, agent="orchestrator")

    for ch in targets:
        t0 = time.time()
        draft = ws.draft_path(PID, VOL, ch)
        n_removed = rollback_memory(ws, ch)
        # 不删旧稿（沙箱禁用 unlink）；produce_chapter 会整体覆盖同一路径
        if draft.exists():
            backup = ROOT / "novel_workspace" / "_harness" / f"old_{ch}.md"
            backup.write_text(draft.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"[ch{ch}] 回滚旧事件 {n_removed} 条，重新生成（tokens={TOKENS}）…")

        gist = ws.outline_chapter_path(PID, VOL, ch)
        gist_text = gist.read_text(encoding="utf-8") if gist.exists() else ""
        sys_prompt = H.build_system_prompt(VOL, ch)
        memories = H.recall(VOL, ch, gist_text, embedding)
        goal = "\n".join([
            f"请撰写第 {VOL} 卷第 {ch} 章正文。",
            "细纲：",
            gist_text[:1200],
            *(["", "前情提要（必须先忆）：", *memories] if memories else []),
            "", "要求：严格按细纲推进，写完本章全部要点，结尾必须是一个完整的收束句。",
        ])
        res = produce_chapter(
            ws, PID, VOL, ch, provider, session=sess, system_prompt=sys_prompt,
            final_goal=goal, prefer_direct=True, generation_tokens=TOKENS, embedding=embedding,
        )
        if not res.ok:
            print(f"    FAILED: {res.result[:200]}")
            continue
        text = draft.read_text(encoding="utf-8")
        print(f"    生成 {len(text)} 字符 / {time.time() - t0:.0f}s")

        events = H.extract_events(provider, text)
        writer = MemoryWriter(ws, PID, embedding=embedding)
        written, conflicts = 0, []
        for i, ev in enumerate(events, 1):
            ids = [H.NAME_TO_ID[n] for n in ev["names"] if n in H.NAME_TO_ID]
            try:
                writer.append_plot_event({
                    "id": f"ev:{PID}:{VOL}:{ch}:r{i}",
                    "at": {"vol": VOL, "ch": ch},
                    "type": ev["kind"], "summary": ev["summary"],
                    "participants": ids, "affected_threads": [],
                })
                for cid in ids:
                    try:
                        writer.append_experience(cid, {"at": {"vol": VOL, "ch": ch},
                                                       "summary": ev["summary"], "state_delta": None})
                    except MemoryConflictError:
                        pass
                written += 1
            except MemoryConflictError as e:
                conflicts.append(str(e)[:80])
        print(f"    编纂：抽取 {len(events)} 入库 {written} 冲突 {len(conflicts)}")

    # 全量重建索引，让回滚后的记忆与索引一致
    idx = MemoryIndex()
    n = idx.rebuild(ws, PID, embedding)
    print(f"\n索引已全量重建：{n} 条碎片")


if __name__ == "__main__":
    main()
