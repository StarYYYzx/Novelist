"""章级事实账本（DL-009-②·2026-10-09，设计哲学：防线进流程而非事后检查）。

C4 类崩法（事件间各自编规则、谎言不自洽、机制前后矛盾——`前两章问题归因报告`
第 4/8 条实证）的预防层：本章此前事件确立的规则/约定/状态变化，以**确定性**方式
注入后续事件 prompt 钉死层（不走 RAG 概率命中——`prompt作用审计.md` 已证明
概率回流不可靠）；章末落盘 `memory/chapter_facts.json`，下一章开头把上一章
末尾事实带入（保留最近 2 章，防跨章断档；卷级连续性仍由 volume_facts 负责）。

本模块只做确定性存取与裁剪，不做任何语义判断（LLM 只在生成时消费这些事实）。
同章重跑幂等：`record()` 替换同章条目而非累加。
"""

from __future__ import annotations

import json

FACTS_REL = "memory/chapter_facts.json"
MAX_ENTRIES = 2          # 只保留最近 2 章（更早的交给 volume_facts）
MAX_FACTS = 12           # 单章事实上限（超出丢最旧——钉死层预算有限）
CARRYOVER = 6            # 下一章开头带入的上一章末尾事实条数


def _path(ws, project_id: str):
    return ws._abs(f"{project_id}/{FACTS_REL}")  # noqa: SLF001 - 与 memory.py 同款路径口径


def load_entries(ws, project_id: str) -> list[dict]:
    """读全部章事实条目（缺失/损坏 → 空列表：账本丢失只降级，不致命、可重建）。"""
    p = _path(ws, project_id)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict)]


def save(ws, project_id: str, entries: list[dict]) -> None:
    ws.write_json(_path(ws, project_id), entries)


def record(entries: list[dict], vol: int, ch: int, facts: list[str]) -> list[dict]:
    """登记/替换当前章事实并裁剪：同章重跑幂等（替换不累加），只留最近 MAX_ENTRIES 章。"""
    clean = [str(f).strip() for f in (facts or []) if str(f).strip()]
    others = [e for e in entries if not (e.get("vol") == vol and e.get("ch") == ch)]
    others.append({"vol": vol, "ch": ch, "facts": clean[-MAX_FACTS:]})
    others.sort(key=lambda e: (int(e.get("vol") or 0), int(e.get("ch") or 0)))
    return others[-MAX_ENTRIES:]


def carryover_lines(entries: list[dict], vol: int, ch: int) -> list[str]:
    """本章开头注入的上一章末尾事实（严格早于 (vol, ch) 的最近一章，末尾 CARRYOVER 条）。"""
    prev = [e for e in entries
            if (int(e.get("vol") or 0), int(e.get("ch") or 0)) < (vol, ch)]
    if not prev:
        return []
    prev.sort(key=lambda e: (int(e.get("vol") or 0), int(e.get("ch") or 0)))
    return [str(f) for f in (prev[-1].get("facts") or [])[-CARRYOVER:]]
