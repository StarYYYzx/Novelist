"""guidan5 项目 build 续跑（target_vol 归一修复后）。

用法：python run_guidan5_resume.py <project_id>
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from novelist.forge import build
from novelist.providers.lmstudio import LMStudioProvider
from novelist.storage.workspace import Workspace

PID = sys.argv[1] if len(sys.argv) > 1 else None
if not PID:
    cands = sorted(p.name for p in (ROOT / "novel_workspace").glob("proj-guidan5-*") if p.is_dir())
    if not cands:
        raise SystemExit("未找到 proj-guidan5-* 项目")
    PID = cands[-1]

provider = LMStudioProvider(
    base_url="http://127.0.0.1:18006",
    model="qwen3.6-35b-a3b-fp8",
    enable_thinking=False,
    reasoning_aware=True,
    timeout_s=1800,
)

ws = Workspace(root=str(ROOT / "novel_workspace"))
print(f"== build 续跑：{PID} ==", flush=True)
bres = build(ws, PID, provider=provider, max_calls=60, resume=True, deepen=True,
             coherence_review=True)
for w in bres.warnings:
    print(f"  [warn] {w}", flush=True)
print(f"build: calls={bres.calls_used} nodes={bres.nodes_done} "
      f"卷={bres.volumes_written} 章={bres.chapters_written} ok={bres.ok} "
      f"gate_halted={bres.gate_halted} pending={bres.pending_review}", flush=True)
print(f"== 完成 {time.strftime('%H:%M:%S')}：细纲见 novel_workspace/{PID}/outline/chapters/ ==", flush=True)
