"""lingyu5 · book 节点定向补跑（resume 会因 bp.volumes 已有 vol1 行跳过 L0 book）。

背景：首轮 build 中 book 节点连续两次 JSON 解析失败（3 卷 artifact 超 2600 输出预算，
已修 nodes.py _node_out_tokens 随卷数放大），回退父层=seed 骨架；vol1 卷纲已出并挂
outline_volume 闸门。本脚本补跑 book 深化 → 落盘 → 标记书级 6 模块 pending。

用法：C:/Python314/python.exe run_lingyu_book_retry.py [pid]
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from novelist.forge.engine import _pack_for_bp, _persist_node  # noqa: SLF001
from novelist.forge.nodes import NodeContext, run_node
from novelist.forge.review import mark_pending, pending_modules
from novelist.forge.state import Blueprint, ForgeState
from novelist.providers.lmstudio import LMStudioProvider
from novelist.storage.workspace import Workspace

PID = None
for a in sys.argv[1:]:
    if a.startswith("proj-"):
        PID = a
if not PID:
    cands = sorted(p.name for p in (ROOT / "novel_workspace").glob("proj-lingyu5-*") if p.is_dir())
    PID = cands[-1]

provider = LMStudioProvider(
    base_url="http://127.0.0.1:18006",
    model="qwen3.6-35b-a3b-fp8",
    enable_thinking=False,
    reasoning_aware=True,
    timeout_s=1800,
)
ws = Workspace(root=str(ROOT / "novel_workspace"))
bp = Blueprint.load(ws, PID)
pack = _pack_for_bp(bp)

print(f"== book 补跑：{PID} ==", flush=True)
ctx = NodeContext(ws=ws, project_id=PID, bp=bp, provider=provider, pack=pack, spec=None)
try:
    res = run_node(ctx, "book")
except Exception as e:  # noqa: BLE001
    print(f"book 补跑失败（保持 seed 骨架继续）: {type(e).__name__}: {e}", flush=True)
    raise SystemExit(1)

_persist_node(ws, PID, "book", res)
bp.data["rev"] = int(bp.data.get("rev") or 1) + 1
bp.save(ws, PID)
from novelist.forge.engine import sync_bible  # noqa: E402

sync_bible(ws, PID, bp)

# 标记书级 6 模块 pending（与 build 内 book 成功后的闸门行为一致）
from novelist.forge.review import REVIEW_MODULES, load_review  # noqa: E402

cfg = load_review(ws, PID)
gated = [m for m, spec in REVIEW_MODULES.items()
         if spec["kind"] == "book" and cfg["switches"].get(m, True)]
mark_pending(ws, PID, bp, gated)
print(f"book ok: decide={res.decide} warns={res.warnings}", flush=True)
print(f"pending={sorted(pending_modules(ws, PID))}", flush=True)

state = ForgeState.load(ws, PID)
state.touch_stage(ws, PID, "review")
state.save(ws, PID)
