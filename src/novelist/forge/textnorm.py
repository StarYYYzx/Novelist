"""角色名防污染归一（ask 槽位 / 节点卡 / 引擎治愈共用）。

真机实证（proj-cloudb5-20260905）：商讨槽位 `characters[role:rival].name` 把整段
反派设定直接写进 name（"沈万钧（华夏特殊部门'镇守司'副司长…）"）→ 蓝图残留污染名
→ 实体/广播按短名匹配失败。本模块供三处共用同一套归一规则。
"""
from __future__ import annotations

import re


def coerce_character_name(raw: str) -> tuple[str, str]:
    """归一角色名。返回 (clean_name, note)；note 为剥离出的注记（可为空）。

    规则与 nodes._coerce_character_card 一致：
    - 括号注记（全角/半角括号、【、[）剥离；
    - 超过 12 字的名按标点截首段，兜底取前 4 字。
    """
    raw = str(raw or "").strip()
    if not raw:
        return "", ""
    name = raw
    note = ""
    m = re.search(r"[（(【\[]", raw)
    if m:
        name = raw[:m.start()].strip(" ，,、；;。")
        note = raw[m.start():].strip("（）()【】[] ，,、；;。")
    if len(name) > 12:
        cut = re.split(r"[，,；;：:。！？是的]", name)[0].strip()
        note = (f"名字原注：{name}" + ("；" + note if note else ""))[:200]
        name = cut if 1 < len(cut) <= 12 else name[:4]
    return name, note


def heal_character_card(card: dict) -> tuple[dict, bool]:
    """就地治愈单张角色卡（name 归一、注记挪入 background）。返回 (card, changed)。"""
    raw = str(card.get("name") or "").strip()
    name, note = coerce_character_name(raw)
    if not name or name == raw:
        return card, False
    card["name"] = name
    if note:
        bg = str(card.get("background") or "").strip()
        card["background"] = (note + ("；" + bg if bg else ""))[:200]
    return card, True


def heal_blueprint_characters(bp) -> list[str]:
    """治愈蓝图中全部污染名（存量项目 build 续跑入口）。返回告警列表。"""
    warns: list[str] = []
    for c in bp.section("characters") or []:
        raw = str(c.get("name") or "")
        _, changed = heal_character_card(c)
        if changed:
            warns.append(f"角色名污染名治愈：{raw[:24]}… → {c.get('name')}")
    return warns
