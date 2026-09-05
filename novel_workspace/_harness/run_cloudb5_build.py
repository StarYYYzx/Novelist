"""云端 B 服务器 5 章链路 · 阶段 2：审核闸门 approve-all → build 续跑（resume）。

用法：python run_cloudb5_build.py <project_id>
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from novelist.forge import build
from novelist.forge.review import REVIEW_MODULES, resolve_pending
from novelist.providers.lmstudio import LMStudioProvider
from novelist.storage.workspace import Workspace

MODEL = "/root/autodl-tmp/models/Qwen3.8-27B-UD-IQ3_S.gguf"


def main() -> None:
    pid = sys.argv[1] if len(sys.argv) > 1 else None
    if not pid:
        cands = sorted(p.name for p in (ROOT / "novel_workspace").glob("proj-cloudb5-*")
                       if p.is_dir())
        if not cands:
            raise SystemExit("未找到 proj-cloudb5-* 项目")
        pid = cands[-1]
    ws = Workspace(root=str(ROOT / "novel_workspace"))
    provider = LMStudioProvider(
        base_url="http://127.0.0.1:16012", model=MODEL,
        enable_thinking=False, reasoning_aware=True, timeout_s=1800)

    for m in REVIEW_MODULES:
        resolve_pending(ws, pid, m, decision="approved", remember=False)
        print(f"approve {m} ✓", flush=True)

    print("== build 续跑（闸门自动循环）==", flush=True)
    t0 = time.time()
    res = None
    for attempt in range(20):
        res = build(ws, pid, provider=provider, max_calls=60, resume=True, deepen=True)
        for w in res.warnings:
            print(f"  [warn] {w}", flush=True)
        print(f"[round {attempt+1}] calls={res.calls_used} nodes={res.nodes_done} "
              f"卷={res.volumes_written} 章={res.chapters_written} ok={res.ok}", flush=True)
        if res.gate_halted:
            for m in res.pending_review:
                resolve_pending(ws, pid, m, decision="approved", remember=False)
                print(f"  auto-approve {m} ✓", flush=True)
            continue
        break
    else:
        print("超过 20 轮循环上限", flush=True)
    print(f"build: calls={res.calls_used} nodes={res.nodes_done} "
          f"卷={res.volumes_written} 章={res.chapters_written} ok={res.ok} "
          f"({time.time()-t0:.0f}s)", flush=True)
    if res.gate_halted:
        print("审核闸门再次挂起:", res.pending_review, flush=True)
        return
    gists = sorted(p.name for p in ws._abs(f"{pid}/outline/chapters").glob("*.md"))  # noqa: SLF001
    print(f"细纲：{gists}", flush=True)
    print("== 完成，可跑 run_cloudb5.py ==" if res.ok else "== build 未完成 ==", flush=True)


if __name__ == "__main__":
    main()
