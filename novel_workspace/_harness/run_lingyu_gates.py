"""lingyu5 项目闸门审批 + build 续跑。

阶段化用法（每次闸门挂起后跑一次）：
    C:/Python314/python.exe run_lingyu_gates.py <pid> book    # 书级 6 模块全过
    C:/Python314/python.exe run_lingyu_gates.py <pid> volume  # 卷纲过
    C:/Python314/python.exe run_lingyu_gates.py <pid> chapter # 章纲过（remember=True）+ 续跑出齐
缺省 pid 取最新 proj-lingyu5-*。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from novelist.forge import build
from novelist.forge.review import pending_modules, resolve_pending
from novelist.providers.lmstudio import LMStudioProvider
from novelist.storage.workspace import Workspace

ARGS = [a for a in sys.argv[1:] if a in ("book", "volume", "chapter")]
PIDS = [a for a in sys.argv[1:] if a.startswith("proj-")]
PID = PIDS[0] if PIDS else None
STAGE = ARGS[-1] if ARGS else "book"
if not PID:
    cands = sorted(p.name for p in (ROOT / "novel_workspace").glob("proj-lingyu5-*") if p.is_dir())
    if not cands:
        raise SystemExit("未找到 proj-lingyu5-* 项目——先跑 run_lingyu_seed.py")
    PID = cands[-1]

provider = LMStudioProvider(
    base_url="http://127.0.0.1:18006",
    model="qwen3.6-35b-a3b-fp8",
    enable_thinking=False,
    reasoning_aware=True,
    timeout_s=1800,
)

ws = Workspace(root=str(ROOT / "novel_workspace"))
pend = pending_modules(ws, PID)
print(f"== {PID} 闸门 stage={STAGE} pending={sorted(pend)} ==", flush=True)

if STAGE == "book":
    for m in [m for m in pend if pend[m].get("node") == "book"]:
        resolve_pending(ws, PID, m, decision="approved", remember=False,
                        note="harness 书级批量通过（人工已阅评审稿）")
        print(f"[gate] book 模块 {m} → approved", flush=True)
elif STAGE == "volume":
    for m in [m for m in pend if pend[m].get("node") == "volume"]:
        resolve_pending(ws, PID, m, decision="approved", remember=False,
                        note="harness 卷纲通过（人工已阅评审稿）")
        print(f"[gate] 卷纲 {m} → approved", flush=True)
elif STAGE == "chapter":
    for m in [m for m in pend if pend[m].get("node") == "chapter"]:
        resolve_pending(ws, PID, m, decision="approved", remember=True,
                        note="harness 章纲通过（remember 免逐章停）")
        print(f"[gate] 章纲 {m} → approved (remember)", flush=True)

print("== build 续跑 ==", flush=True)
bres = build(ws, PID, provider=provider, max_calls=90, resume=True, deepen=True,
             coherence_review=True)
for w in bres.warnings:
    print(f"  [warn] {w}", flush=True)
print(f"build: calls={bres.calls_used} nodes={bres.nodes_done} "
      f"卷={bres.volumes_written} 章={bres.chapters_written} ok={bres.ok} "
      f"gate_halted={bres.gate_halted} pending={bres.pending_review}", flush=True)
