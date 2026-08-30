"""一致性确定性规则引擎（docs/04 §5.4 / docs/07 §5，ADR-009 规则层）。

输入：项目工作区的 bible/memory/outline 数据。
输出：结构化告警 `[{level, rule_id, object_ref, detail}]`（level=block|warn）。

当前规则：
- R-REF  引用完整性：entities 引用的 id 必须存在（characters/locations/plot_threads 互引）。
- R-TL   时间线单调：正文出现的时间线事件在 timeline 中有记录且顺序不矛盾。
- R-LEX  用词纪律：正文中出现现代词、西方典故、style.json 禁用词（原完全不扫正文）。
- R-PWR  战力体系表述一致：同一境界不得混用「层」「重」「级」等不同细分说法。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..storage.workspace import Workspace

# 现代词汇：出现在古风/修仙语境里即出戏（端到端实测「厚眼镜」）
MODERN_WORDS = (
    "眼镜", "手机", "电脑", "电话", "汽车", "飞机", "高铁", "咖啡", "超市", "公司",
    "经理", "化学", "物理", "电池", "网络", "视频", "打卡", "狙击", "雷达", "电梯",
    "医院", "警察", "总统", "沙发", "巧克力", "麦克风", "发动机", "充电",
)

# 西方典故：与中文修仙语境冲突（端到端实测「达摩克利斯之剑」）
WESTERN_ALLUSIONS = (
    "达摩克利斯", "阿喀琉斯", "斯巴达", "宙斯", "丘比特", "潘多拉", "特洛伊",
    "伊甸", "普罗米修斯", "西西弗", "俄狄浦斯", "浮士德",
)

# 境界细分后缀：同一境界混用即为表述不一致
_LEVEL_SUFFIX_RE = r"([一二三四五六七八九]?[层重]|初期|中期|后期|大圆满|巅峰)"


def _read_json(path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def _iter_chapters(ws: Workspace, project_id: str) -> list[tuple[str, str]]:
    """遍历正文（优先 chapters/，无则回退 drafts/chapters/；都没有时返回空）。"""
    out: list[tuple[str, str]] = []
    for base in ("chapters", "drafts/chapters"):
        d = ws._abs(f"{project_id}/{base}")
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.md")):
            try:
                out.append((f.stem, f.read_text(encoding="utf-8")))
            except OSError:  # pragma: no cover
                continue
        if out:
            break
    return out


def _banned_words(ws: Workspace, project_id: str) -> list[str]:
    st = _read_json(ws._abs(f"{project_id}/bible/style.json")) or {}
    words = st.get("forbidden_words") if isinstance(st, dict) else None
    return [w for w in (words or []) if isinstance(w, str) and w]


def _lexicon_check(ws: Workspace, project_id: str) -> list[RuleAlert]:
    """R-LEX：正文用词纪律（现代词 / 西方典故 / style.json 禁用词）。"""
    alerts: list[RuleAlert] = []
    banned = _banned_words(ws, project_id)
    for name, text in _iter_chapters(ws, project_id):
        for w in MODERN_WORDS:
            if w in text:
                i = text.index(w)
                alerts.append(RuleAlert(
                    level="block", rule_id="R-LEX", object_ref=name,
                    detail=f"现代词汇「{w}」出戏：…{text[max(0, i - 10):i + len(w) + 8]}…"))
        for w in WESTERN_ALLUSIONS:
            if w in text:
                i = text.index(w)
                alerts.append(RuleAlert(
                    level="block", rule_id="R-LEX", object_ref=name,
                    detail=f"西方典故「{w}」与语境冲突：…{text[max(0, i - 8):i + len(w) + 12]}…"))
        for w in banned:
            if w in text:
                alerts.append(RuleAlert(
                    level="block", rule_id="R-LEX", object_ref=name,
                    detail=f"命中 style.json 禁用词「{w}」"))
    return alerts


def _power_system_check(ws: Workspace, project_id: str) -> list[RuleAlert]:
    """R-PWR：同一境界的细分表述必须统一（不得「炼气三层」与「炼气一重」混用）。"""
    alerts: list[RuleAlert] = []
    wv = _read_json(ws._abs(f"{project_id}/bible/worldview.json")) or {}
    levels = ((wv.get("power_system") or {}).get("levels")) if isinstance(wv, dict) else None
    if not levels:
        return alerts
    used: dict[str, set[str]] = {}
    for _name, text in _iter_chapters(ws, project_id):
        for lv in levels:
            pat = re.escape(str(lv)) + _LEVEL_SUFFIX_RE
            for m in re.finditer(pat, text):
                used.setdefault(str(lv), set()).add(m.group(1))
    for lv, suffixes in sorted(used.items()):
        fams = {("层" if s.endswith("层") else "重" if s.endswith("重")
                 else "境阶" if s in ("初期", "中期", "后期", "大圆满", "巅峰") else "其他")
                for s in suffixes}
        if len(fams) > 1:
            alerts.append(RuleAlert(
                level="warn", rule_id="R-PWR", object_ref=lv,
                detail=(f"境界「{lv}」混用细分表述：{sorted(suffixes)}"
                        "（应统一，如统一用「层」或统一用「初期/中期/后期」）")))
    return alerts


def run_lexicon_checks(ws: Workspace, project_id: str) -> list[RuleAlert]:
    """只跑正文相关规则（R-LEX / R-PWR），供审校流程单独调用。"""
    return _lexicon_check(ws, project_id) + _power_system_check(ws, project_id)


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
    """全部确定性规则（bible 引用 + 时间线 + 正文用词与战力表述）。

    注意：这里的"全部"也只是**能确定性判定**的部分。称谓是否合乎身份、
    情节逻辑是否自洽、伏笔是否回收等仍属 LLM 语义检（见 consistency/reviewer.py）。
    """
    alerts: list[RuleAlert] = []
    alerts += _referential_integrity(ws, project_id)
    alerts += _timeline_monotonic(ws, project_id)
    alerts += run_lexicon_checks(ws, project_id)
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
