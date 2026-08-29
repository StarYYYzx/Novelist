"""一致性确定性规则引擎（docs/04 §5.4 / docs/07 §5，ADR-009 规则层）。

输入：项目工作区的 bible/memory/outline 数据。
输出：结构化告警 `[{level, rule_id, object_ref, detail}]`（level=block|warn）。

当前规则：
- R-REF  引用完整性：entities 引用的 id 必须存在（characters/locations/plot_threads 互引）。
- R-TL   时间线单调：正文出现的时间线事件在 timeline 中有记录且顺序不矛盾。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..storage.workspace import Workspace


@dataclass
class RuleAlert:
    level: str          # block | warn
    rule_id: str
    object_ref: str
    detail: str


def _load(ws: Workspace, project_id: str, rel: str) -> dict | list:
    p = ws._abs(f"{project_id}/{rel}")
    if not p.exists():
        return {}
    import json

    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):  # pragma: no cover
        return {}


def run_rule_checks(ws: Workspace, project_id: str) -> list[RuleAlert]:
    alerts: list[RuleAlert] = []
    alerts += _referential_integrity(ws, project_id)
    alerts += _timeline_monotonic(ws, project_id)
    return alerts


def _referential_integrity(ws: Workspace, project_id: str) -> list[RuleAlert]:
    """校验实体 id 引用完整（docs/06 §5.2）。人物 relationship.target 须命中已有人物。"""
    alerts: list[RuleAlert] = []
    chars = _load(ws, project_id, "bible/characters.json")
    if not isinstance(chars, list):
        return alerts
    char_ids = {c.get("id") for c in chars if isinstance(c, dict) and c.get("id")}
    for c in chars:
        if not isinstance(c, dict):
            continue
        cid = c.get("id")
        for rel in c.get("relationships") or []:
            target = rel.get("target") if isinstance(rel, dict) else None
            if target and target not in char_ids:
                alerts.append(
                    RuleAlert(
                        level="block",
                        rule_id="R-REF",
                        object_ref=cid or "?",
                        detail=f"人物 {cid} 引用了不存在的人物 {target}",
                    )
                )
        first = c.get("first_appear")
        if first and not isinstance(first, dict):
            alerts.append(RuleAlert(level="warn", rule_id="R-REF", object_ref=cid or "?",
                                    detail="first_appear 必须为 {vol,ch}"))
    return alerts


def _timeline_monotonic(ws: Workspace, project_id: str) -> list[RuleAlert]:
    """校验时间线事件按 (vol, ch) 出现顺序不倒退（docs/06 §3.3）。"""
    alerts: list[RuleAlert] = []
    tls = _load(ws, project_id, "bible/timeline.json")
    if not isinstance(tls, list):
        return alerts
    prev = None
    for t in tls:
        if not isinstance(t, dict):
            continue
        chaps = t.get("in_chapters") or []
        # 采用该事件最早出现的章节作为排序基准
        if chaps and isinstance(chaps[0], dict):
            cur = (int(chaps[0].get("vol", 0)), int(chaps[0].get("ch", 0)))
            if prev is not None and cur < prev:
                alerts.append(
                    RuleAlert(
                        level="block",
                        rule_id="R-TL",
                        object_ref=t.get("id", "?"),
                        detail=f"时间线事件 {t.get('id')} 出现顺序({cur[0]}:{cur[1]}) 早于前一事件({prev[0]}:{prev[1]})",
                    )
                )
            prev = cur
        else:
            alerts.append(RuleAlert(level="warn", rule_id="R-TL", object_ref=t.get("id", "?"),
                                    detail="时间线事件缺少 in_chapters 章节定位"))
    return alerts


def sort_alerts(alerts: list[RuleAlert]) -> list[RuleAlert]:
    import operator

    return sorted(alerts, key=operator.attrgetter("object_ref", "rule_id"))


def to_dict(alerts: list[RuleAlert]) -> list[dict]:
    return [{"level": a.level, "rule_id": a.rule_id, "object_ref": a.object_ref, "detail": a.detail} for a in alerts]
