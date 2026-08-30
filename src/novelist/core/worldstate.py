"""世界状态层（B-STATE，人工审查第三批第 3 条）。

## 它解决什么

LLM 记不住可数值化的状态。v2 评阅实测：第 2 章苏晚（炼气三层）被赵虎（炼气五层）
按进泥里，第 3 章却一招断其手腕——**战力跳级**。规则引擎 R-REF/R-LEX 不看战力，
9B 审校师对跨章逻辑漏报。这类错误不能靠"让模型记住"，只能靠确定性状态机。

`bible/worldstate.json` 记录每个人物的**当前**修为 / 位置 / 持有物 / 伤势，
由编纂员在事件回写时同步更新（`MemoryWriter.append_experience` 的 `state_delta`
字段此前一直传 None，本模块把这条现成的接口接上），生成时注入"人物当前状态"，
一致性引擎用 R-STATE 校验。

设计原则与 ADR-016 一致：worldstate.json 是**事实源**（不是缓存），
但它可以从 bible/characters.json 重建初始值，所以删掉后损失的是"小说进行到的状态"，
需重新编纂——因此**不入可再生缓存，随项目走**。

## 状态行格式（编纂员 LLM 输出）

```
状态：苏晚 | 修为：炼气四层 | 位置：藏经阁 | 获得：青冥诀残篇 | 受伤：经脉灼伤
```

键支持：修为/境界、位置、获得、失去、受伤、痊愈、阵亡。解析容错：缺哪项就只更新哪项。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

WORLDSTATE_REL = "bible/worldstate.json"

# 状态行里允许的键 -> apply_delta 处理分支
_STATE_KEYS = {
    "修为": "realm",
    "境界": "realm",
    "位置": "location",
    "所在地": "location",
    "获得": "items_add",
    "失去": "items_remove",
    "受伤": "injuries_add",
    "痊愈": "injuries_remove",
    "阵亡": "dead",
    "死亡": "dead",
}

_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_SUB_ORDER = {"初期": 1, "中期": 2, "后期": 3, "大圆满": 4, "巅峰": 5}


def parse_realm(realm: str, levels: list[str]) -> tuple[int, int] | None:
    """把「炼气三层」「筑基大圆满」解析成（大境界序号, 细分序号），用于单调性比较。

    无法识别细分时细分记 0（只比较大境界）；完全不在体系内返回 None（R-STATE 跳过）。
    """
    if not realm:
        return None
    for i, lv in enumerate(levels):
        if not realm.startswith(lv):
            continue
        sub = realm[len(lv):].strip()
        m = re.match(r"^([一二三四五六七八九])[层重]$", sub)
        if m:
            return (i, _CN_NUM[m.group(1)])
        if sub in _SUB_ORDER:
            return (i, _SUB_ORDER[sub])
        return (i, 0)
    return None


def _read(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def load(ws, project_id: str) -> dict:
    """读世界状态；不存在时返回空结构（不自动初始化，由编纂/脚本显式 init）。"""
    data = _read(ws._abs(f"{project_id}/{WORLDSTATE_REL}")) or {}
    data.setdefault("characters", {})
    return data


def save(ws, project_id: str, data: dict) -> None:
    ws.write_json(ws._abs(f"{project_id}/{WORLDSTATE_REL}"), data)


def init_from_bible(ws, project_id: str) -> dict:
    """从 bible/characters.json 初始化（power.level → realm），不覆盖已有状态。"""
    chars = _read(ws._abs(f"{project_id}/bible/characters.json")) or []
    state = load(ws, project_id)
    for c in chars if isinstance(chars, list) else []:
        if not isinstance(c, dict) or not c.get("id"):
            continue
        cur = state["characters"].setdefault(c["id"], {})
        cur.setdefault("name", c.get("name", ""))
        pw = c.get("power") or {}
        cur.setdefault("realm", pw.get("level", ""))
        cur.setdefault("location", "")
        cur.setdefault("items", [])
        cur.setdefault("injuries", [])
        cur.setdefault("dead", c.get("status") == "dead")
        cur.setdefault("history", [])
    save(ws, project_id, state)
    return state


def apply_delta(ws, project_id: str, char_id: str, delta: dict, at: dict | None = None) -> dict:
    """把一次状态变更合并进世界状态，并记录历史（R-STATE 依据历史校验单调性）。"""
    state = load(ws, project_id)
    cur = state["characters"].setdefault(char_id, {"name": "", "realm": "", "location": "",
                                                   "items": [], "injuries": [],
                                                   "dead": False, "history": []})
    recorded = {}
    for key, value in (delta or {}).items():
        field = _STATE_KEYS.get(key, key)
        value = str(value or "").strip()
        if not value:
            continue
        if field == "realm":
            if value and value != cur.get("realm"):
                cur["realm"], recorded["realm"] = value, value
        elif field == "location":
            if value != cur.get("location"):
                cur["location"], recorded["location"] = value, value
        elif field == "items_add":
            if value not in cur["items"]:
                cur["items"].append(value)
                recorded["获得"] = value
        elif field == "items_remove":
            if value in cur["items"]:
                cur["items"].remove(value)
                recorded["失去"] = value
        elif field == "injuries_add":
            if value not in cur["injuries"]:
                cur["injuries"].append(value)
                recorded["受伤"] = value
        elif field == "injuries_remove":
            if value in cur["injuries"]:
                cur["injuries"].remove(value)
                recorded["痊愈"] = value
        elif field == "dead":
            cur["dead"] = value in ("是", "true", "True", "1", "死亡", "阵亡")
            recorded["阵亡"] = cur["dead"]
    if recorded:
        cur["history"].append({"at": dict(at or {}), "delta": recorded})
    save(ws, project_id, state)
    return recorded


def snapshot_lines(state: dict, char_ids: list[str] | None = None) -> list[str]:
    """人物当前状态行（注入生成上下文用）。`state` 为 load()/init_from_bible() 的返回值。"""
    lines = []
    for cid, cur in (state.get("characters") or {}).items():
        if not isinstance(cur, dict):
            continue
        if char_ids and cid not in char_ids:
            continue
        bits = [f"{cur.get('name') or cid}"]
        if cur.get("realm"):
            bits.append(f"当前修为 {cur['realm']}")
        if cur.get("location"):
            bits.append(f"所在地 {cur['location']}")
        if cur.get("items"):
            bits.append("持有 " + "、".join(cur["items"]))
        if cur.get("injuries"):
            bits.append("伤势 " + "、".join(cur["injuries"]))
        if cur.get("dead"):
            bits.append("已死亡（不得再出场行动）")
        if len(bits) > 1:
            lines.append("- " + "，".join(bits))
    return lines


# ---------------------------------------------------------------- 状态行解析

_LINE_RE = re.compile(r"^状态[:：]\s*(.+)$")


def parse_state_lines(text: str, name_to_id: dict[str, str]) -> list[tuple[str, dict]]:
    """从编纂员输出里解析状态行。返回 [(char_id, delta)]。"""
    out: list[tuple[str, dict]] = []
    for raw in text.splitlines():
        m = _LINE_RE.match(raw.strip())
        if not m:
            continue
        parts = [p.strip() for p in m.group(1).split("|") if p.strip()]
        if not parts:
            continue
        cid = name_to_id.get(parts[0]) or next(
            (v for k, v in name_to_id.items() if k and k in parts[0]), None)
        if not cid:
            continue
        delta: dict = {}
        for seg in parts[1:]:
            pieces = re.split(r"[:：]", seg, maxsplit=1)
            if len(pieces) < 2:
                continue
            k, v = pieces[0].strip(), pieces[1].strip()
            if k in _STATE_KEYS and v:
                delta[k] = v
        if delta:
            out.append((cid, delta))
    return out
