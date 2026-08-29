"""扫描正文里出现的、圣经未建档的人物名（改进版）。

规则引擎的 R-REF 只校验 `characters.*.relationships[].target` 是否命中已有人物 id，
**完全不扫正文**。因此模型在正文里凭空造人（如第一次 CLI 直出时出现的「李长风」）
不会触发任何告警。本脚本补足这项检查。

判定方式：在角色称谓词（长老/师兄/师姐/…）前取 4 字窗口，若窗口内包含任一圣经人物
的姓名或别称，视为已建档；否则把窗口末尾 2–3 字作为「疑似未建档人物」候选，供人工复核。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PID = "proj-duanyu"
BASE = Path(__file__).resolve().parents[1] / PID
bible = json.loads((BASE / "bible/characters.json").read_text(encoding="utf-8"))
known = [c["name"] for c in bible] + [a for c in bible for a in (c.get("aliases") or [])]
known = sorted(set(known), key=len, reverse=True)

ROLES = "长老|师兄|师姐|师弟|师妹|宗主|护法|老祖|侯|前辈|道友|真人|散人|剑主|丹师"

candidates: dict[str, set[str]] = {}
for f in sorted((BASE / "chapters").glob("*.md")):
    text = f.read_text(encoding="utf-8")
    for m in re.finditer(r"(.{0,4})(?:" + ROLES + r")", text):
        win = m.group(1)
        if not win.strip():
            continue
        if any(k in win for k in known):
            continue
        tail = win[-3:].lstrip("的了他她是我与和及、，。：")
        if len(tail) >= 2:
            candidates.setdefault(tail, set()).add(f.stem)

print("=== 疑似未建档人物（需人工复核）===")
if not candidates:
    print("  无")
for k, v in sorted(candidates.items()):
    print(f"  「{k}」  出现在 {sorted(v)}")

print()
print("=== 圣经已建档且正文确有出场的人物 ===")
all_text = "".join(f.read_text(encoding="utf-8") for f in sorted((BASE / "chapters").glob("*.md")))
appeared = [c["name"] for c in bible if c["name"] in all_text]
print(f"  {len(appeared)}/{len(bible)}：" + "、".join(appeared))
print("  未出场：" + "、".join(c["name"] for c in bible if c["name"] not in all_text))
