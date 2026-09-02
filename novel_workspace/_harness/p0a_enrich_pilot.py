"""P0-A 试点：对 yelan3 三张代表性缺料卡跑 DeepSeek 提案（M3r）。

试点卡：李慕白(char_li 首席·情敌) / 苏婉(char_su 药堂暗线) / 秦叔(char_qin 绑定对象)。
只写 bible/characters_enrich_pending.json，绝不碰 characters.json。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from novelist.core.character_enrich import load_pending, propose  # noqa: E402
from novelist.providers.deepseek import DeepSeekProvider  # noqa: E402
from novelist.storage.workspace import Workspace  # noqa: E402

WS_ROOT = Path(__file__).resolve().parents[1]  # novel_workspace/
PID = "proj-yelan3"
TARGETS = ["char:li", "char:su", "char:qin"]  # 李慕白/苏婉/秦叔

ws = Workspace(root=str(WS_ROOT))
llm = DeepSeekProvider(model="deepseek-chat")
results = propose(ws, PID, llm, card_ids=TARGETS)
for r in results:
    tag = "OK " if r.get("ok") and r.get("proposed") else ("skip" if r.get("ok") else "FAIL")
    why = f" —— {r['reasons']}" if r.get("reasons") else ""
    print(f"[{tag}] {r.get('name')} ({r.get('card_id')}){why}")

pending = load_pending(ws, PID)
print("\n===== pending 现状 =====")
for p in pending:
    if p.get("card_id") in TARGETS:
        print(f"\n## {p['card_name']} 缺 {p.get('missing')}")
        print(f"  age: {p.get('age')}")
        for r in p.get("relationships") or []:
            print(f"  rel: {r.get('target')} — {r.get('type')}")
        for b in p.get("behavior_rules") or []:
            print(f"  rule: {b}")
