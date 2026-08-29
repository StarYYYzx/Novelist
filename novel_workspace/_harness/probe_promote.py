"""探查：草稿转正（promote）的门禁路径与 CLI 入口。

系统整体测试的一部分——验证 F6.1「危险/重要操作走门禁」在 promote 上是否成立，
以及是否存在把 drafts/ 转正式 chapters/ 的 CLI 路径（docs/06 §4.2 的 reviewed_ok → published）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from novelist.cli import cli  # noqa: E402
from novelist.core.session import SessionInfo  # noqa: E402
from novelist.core.tools import PermissionGate  # noqa: E402
from novelist.storage.workspace import Workspace  # noqa: E402
from novelist.tools import build_registry  # noqa: E402

PID = "proj-duanyu"
ws = Workspace(root=str(ROOT / "novel_workspace"))
sess = SessionInfo(project_id=PID, agent="orchestrator")

print("=== 1) 默认 supervised 策略下调用 promote_draft（sensitive）===")
reg = build_registry(ws)
try:
    r = reg.invoke(sess, "promote_draft", {"vol": 1, "ch": 1})
    print("   结果:", r.status, r.code, r.data)
except Exception as e:  # noqa: BLE001
    print("   抛出:", type(e).__name__, e)

print()
print("=== 2) 策略把 sensitive 设为 allow 后 ===")
gate = PermissionGate(profiles={"supervised": {"sensitive": "allow", "danger": "deny", "tools": {}}})
r2 = build_registry(ws, gate=gate).invoke(sess, "promote_draft", {"vol": 1, "ch": 1})
print("   结果:", r2.status, r2.code, r2.data)
print("   正式章节已生成:", ws.chapter_path(PID, 1, 1).exists())

print()
print("=== 3) CLI 命令清单 ===")
cmds = sorted(cli.commands.keys())
print("   ", cmds)
print("   含 promote/publish 入口:", any(k in ("promote", "publish") for k in cmds))
