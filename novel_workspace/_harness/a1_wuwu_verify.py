# -*- coding: utf-8 -*-
"""A1 wuwu 条款落盘验证：契约校验 + RAG 命中 + rules 注入 + director 卡行。

数据改动：proj-yelan3/bible/{settings,worldview,skills}.json（gitignored 测试数据）。
只读校验，不落盘。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from novelist.core.bible import validate_project          # noqa: E402
from novelist.core.context import build_system_prompt, load_bible  # noqa: E402
from novelist.core.director import render_card_line, load_characters  # noqa: E402
from novelist.core.knowledge import KnowledgeBase         # noqa: E402


class FakeWs:
    """只提供 _abs 的最小桩（测试路径用）。"""

    def __init__(self, base: str) -> None:
        self._base = Path(base)

    def _abs(self, rel: str) -> Path:
        return self._base / rel


BASE = "novel_workspace"          # Workspace root（与 broadcast_probe_ch6.py 同口径）
PID = "proj-yelan3"               # project_id：真源 = novel_workspace/proj-yelan3/bible/
fws = FakeWs(BASE)

# 1) 契约校验（settings/skills/worldview schema 全过）
v = validate_project(fws, PID)
print("contract violations:", len(v))
for x in v[:5]:
    print("  ", x.path, x.errors)

# 2) RAG：绑定场景事件 → 应命中 set:wuwu 全文条款 + skill:wuwu note
kb = KnowledgeBase(fws, PID)
ev = "叶岚借着躬身行礼，指尖掠过老祖袖口，绑定化神后期，参透残页运劲"
hits = kb.keyword_hits(ev)
wuwu = [i for i in hits if i.id == "set:wuwu"]
print("keyword_hits set:wuwu 命中:", bool(wuwu), "| 共", len(hits), "项")
if wuwu:
    txt = wuwu[0].payload.get("text", "")
    for key in ("接触", "主动解绑", "无冷却", "对象身亡", "无感"):
        assert key in txt, key
    print("  条款全文注入, 长度:", len(txt))
sk = [i for i in hits if i.id == "skill:wuwu"]
print("skill:wuwu 命中:", bool(sk))
if sk:
    print("  note:", sk[0].payload.get("note", "")[:80])

# 3) rules 注入（system prompt 每章全量，零召回风险）
bible = load_bible(fws, PID)
sp = build_system_prompt(bible, [], 1, 7)
assert "接触即绑一人" in sp
print("worldview.rules 已入 system prompt: True")

# 4) director 无条件人物行带 behavior_rules（叶岚机制纪律可达）
chars = load_characters(fws, "proj-yelan3")
yelan = next(c for c in chars if c["id"] == "char:yelan")
line = render_card_line(yelan)
print("director yelan 行含 行为:", "行为：" in line)
print()
print("OK")
