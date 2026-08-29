"""扫描正文里的世界观/文风违反：现代词汇、西方典故、战力体系矛盾。

系统无任何对应检查：`ModerationPrechecker` 只做敏感词子串匹配且**词表默认为空**；
`consistency` 只有 R-REF（引用完整性）与 R-TL（时间线单调）两条规则，不碰文风与世界观。
docs/05 定义的「审校师」子代理（LLM 语义检）未实现。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

PID = "proj-duanyu"
BASE = Path(__file__).resolve().parents[1] / PID

MODERN = ["眼镜", "手机", "电脑", "电话", "汽车", "飞机", "咖啡", "超市", "公司", "经理",
          "化学", "物理", "能量守恒", "狙击", "雷达", "电池", "网络", "视频", "打卡"]
WESTERN = ["达摩克利斯", "阿喀琉斯", "斯巴达", "宙斯", "丘比特", "潘多拉", "特洛伊", "伊甸"]
TONE_BREAK = ["系统", "签到", "叮，", "宿主", "面板"]

LEVELS = ["炼气", "筑基", "金丹", "元婴", "化神"]

print("=== 现代词汇 ===")
hit = False
for f in sorted((BASE / "chapters").glob("*.md")):
    t = f.read_text(encoding="utf-8")
    for w in MODERN:
        for m in re.finditer(re.escape(w), t):
            hit = True
            print(f"  {f.stem}: 「{w}」 …{t[max(0, m.start()-18):m.start()+len(w)+10]}…".replace("\n", " "))
if not hit:
    print("  无")

print("\n=== 西方典故 ===")
hit = False
for f in sorted((BASE / "chapters").glob("*.md")):
    t = f.read_text(encoding="utf-8")
    for w in WESTERN:
        for m in re.finditer(re.escape(w), t):
            hit = True
            print(f"  {f.stem}: 「{w}」 …{t[max(0, m.start()-14):m.start()+len(w)+16]}…".replace("\n", " "))
if not hit:
    print("  无")

print("\n=== 网文系统流黑话（style.json 禁用词）===")
style = json.loads((BASE / "bible/style.json").read_text(encoding="utf-8"))
banned = style.get("forbidden_words") or []
hit = False
for f in sorted((BASE / "chapters").glob("*.md")):
    t = f.read_text(encoding="utf-8")
    for w in banned:
        if w in t:
            hit = True
            print(f"  {f.stem}: 「{w}」")
if not hit:
    print(f"  无（禁用词表：{'、'.join(banned)}）")

print("\n=== 战力体系：正文提到的境界 ===")
found = set()
for f in sorted((BASE / "chapters").glob("*.md")):
    t = f.read_text(encoding="utf-8")
    for lv in LEVELS:
        for m in re.finditer(re.escape(lv), t):
            seg = t[m.start(): m.start() + 6]
            found.add(seg)
print("  " + "、".join(sorted(found)))

print("\n=== 系统自带检查覆盖不到的部分（罗列）===")
for line in [
    "人物称谓与身份是否匹配（如称炼气三层的少年为『前辈』）",
    "境界/战力描写是否越级或自相矛盾",
    "世界观铁律是否被违反（如『修士不可对凡人出手』）",
    "大纲覆盖度（细纲 key_events 是否都写进正文）",
    "跨章连续性（前章伏笔、人物伤势、物品去向）",
]:
    print("  - " + line)
