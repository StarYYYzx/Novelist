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


def _modern_words(ws: Workspace, project_id: str) -> tuple[str, ...]:
    """现代词表按类型可配：都市/现代背景里「电梯/手机/公司」都正常。

    默认词表是修仙/古风导向（实测「厚眼镜」出戏）；worldview.json 可给
    `modern_words` 覆盖（都市文传空列表，只留真正出戏的词）。
    """
    wv = _read_json(ws._abs(f"{project_id}/bible/worldview.json")) or {}
    if isinstance(wv, dict) and isinstance(wv.get("modern_words"), list):
        return tuple(str(x) for x in wv["modern_words"])
    return MODERN_WORDS


def _lexicon_check(ws: Workspace, project_id: str) -> list[RuleAlert]:
    """R-LEX：正文用词纪律（现代词 / 西方典故 / style.json 禁用词）。"""
    alerts: list[RuleAlert] = []
    banned = _banned_words(ws, project_id)
    modern = _modern_words(ws, project_id)
    for name, text in _iter_chapters(ws, project_id):
        for w in modern:
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
    """只跑正文相关规则（R-LEX / R-PWR / R-ITEM），供审校流程单独调用。"""
    return (_lexicon_check(ws, project_id) + _power_system_check(ws, project_id)
            + _item_check(ws, project_id))


@dataclass
class RuleAlert:
    level: str          # block | warn
    rule_id: str
    object_ref: str
    detail: str


def _item_check(ws: Workspace, project_id: str) -> list[RuleAlert]:
    """R-ITEM：注册表物品/功法的正文一致性（讨论决策——物品无唯一身份是奖励倒退的根因）。

    - 同一物品在正文出现 ≥2 种叫法（规范名 + 别名/带状态形态）→ warn（同物异名）
    - 正文出现注册表物品，但 worldstate 没有任何人物持有/曾持有 → warn（凭空出现；
      若为首次获得的当前章，编纂尚未回写属正常，warn 供人工复核）
    """
    try:
        from ..core.registry import Registry
    except Exception:  # pragma: no cover
        return []
    alerts: list[RuleAlert] = []
    reg = Registry.load(ws, project_id)
    if not reg.all_entries():
        return alerts
    full = "\n".join(text for _n, text in _iter_chapters(ws, project_id))
    st = _read_json(ws._abs(f"{project_id}/bible/worldstate.json")) or {}
    chars = st.get("characters") if isinstance(st, dict) else {}
    held: set[str] = set()
    for cur in (chars or {}).values():
        if isinstance(cur, dict):
            for x in cur.get("items") or []:
                if isinstance(x, str):
                    held.add(x)
    for e in reg.all_entries():
        forms = {e.name}
        if e.state:
            forms.add(f"{e.name}{e.state}")
            forms.add(f"{e.name}（{e.state}）")
        forms.update(a for a in e.aliases if a)
        # 同物异名：规范名与「别名/带状态形态」同时出现。
        # 关键：别名可能是规范名的子串（「忘情录」⊂「太上忘情录」），
        # 直接子串匹配会误报——先剔除全部规范名出现，再查其他形态。
        stripped = full.replace(e.name, "")
        other_forms = [f for f in forms if f != e.name and f and f in stripped]
        if e.name in full and other_forms:
            alerts.append(RuleAlert(
                level="warn", rule_id="R-ITEM", object_ref=e.name,
                detail=f"同一物品出现多种叫法：{sorted({e.name, *other_forms})}"
                       f"（应统一用注册表规范名「{e.name}」）"))
        # 技能/功法条目不查"持有"（技能是学会的不是持有的，正文出现正常）
        if e.name not in held and e.name in full and e.type not in (
                "cultivation", "secret", "technique", "combat"):
            alerts.append(RuleAlert(
                level="warn", rule_id="R-ITEM", object_ref=e.name,
                detail=f"正文出现「{e.name}」，但 worldstate 无任何人持有/曾持有。"
                       "若为本章首次获得，编纂回写后应出现；否则是凭空获得或漏记"))
    return alerts


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
    """全部确定性规则（bible 引用 + 时间线 + 正文用词与战力表述 + 世界状态）。

    注意：这里的"全部"也只是**能确定性判定**的部分。称谓是否合乎身份、
    情节逻辑是否自洽、伏笔是否回收等仍属 LLM 语义检（见 consistency/reviewer.py）。
    """
    alerts: list[RuleAlert] = []
    alerts += _referential_integrity(ws, project_id)
    alerts += _timeline_monotonic(ws, project_id)
    alerts += run_lexicon_checks(ws, project_id)
    alerts += run_state_checks(ws, project_id)
    return alerts


def _worldstate_check(ws: Workspace, project_id: str) -> list[RuleAlert]:
    """R-STATE：世界状态层的确定性校验（人工审查第三批第 3 条）。

    - 境界只进不退（倒退 → block；"跌境/自废修为"类情节当前不做例外，出现时人工仲裁）
    - 一次跨 2 个及以上大境界 → warn（须有对应突破/机缘描写，否则是编纂员抽错了）
    - 已死亡人物在死亡章之后仍出现在正文 → warn（可能是回忆/提及，人工复核）
    """
    from ..core.worldstate import parse_realm

    alerts: list[RuleAlert] = []
    wv = _read_json(ws._abs(f"{project_id}/bible/worldview.json")) or {}
    levels = ((wv.get("power_system") or {}).get("levels")) if isinstance(wv, dict) else None
    st = _read_json(ws._abs(f"{project_id}/bible/worldstate.json")) or {}
    chars = st.get("characters") if isinstance(st, dict) else None
    if not isinstance(chars, dict):
        return alerts

    chapters = {name: text for name, text in _iter_chapters(ws, project_id)}

    # 基线：人物卡 power.level（init 前的初始状态，未进 history，须作为比较起点）
    card_realms: dict[str, str] = {}
    card_data = _read_json(ws._abs(f"{project_id}/bible/characters.json")) or []
    for c in card_data if isinstance(card_data, list) else []:
        if isinstance(c, dict) and c.get("id") and (c.get("power") or {}).get("level"):
            card_realms[c["id"]] = str(c["power"]["level"])

    for cid, cur in chars.items():
        if not isinstance(cur, dict):
            continue
        name = cur.get("name") or cid
        seq = [h for h in (cur.get("history") or [])
               if isinstance(h, dict) and isinstance(h.get("at"), dict)]
        seq.sort(key=lambda h: (int(h["at"].get("vol", 0) or 0), int(h["at"].get("ch", 0) or 0)))

        # 初始修为（人物卡）作为单调性比较的基线
        prev: tuple[int, int] | None = None
        if levels and card_realms.get(cid):
            prev = parse_realm(card_realms[cid], [str(x) for x in levels])
        death_ch: tuple[int, int] | None = None
        for h in seq:
            delta = h.get("delta") or {}
            at = (int(h["at"].get("vol", 0) or 0), int(h["at"].get("ch", 0) or 0))
            if delta.get("阵亡") or delta.get("死亡"):
                death_ch = at
            new_realm = delta.get("realm")
            if not new_realm or not levels:
                continue
            parsed = parse_realm(str(new_realm), [str(x) for x in levels])
            if parsed is None:
                continue
            if prev is not None:
                # 倒退判定要区分「大境界」与「细分」：
                # - 大境界倒退（a < p）→ block；
                # - 细分倒退（a == p 且 b < q）→ 仅当双方都有细分才判（无细分如「内劲」
                #   可能是「内劲中期」的简写，误判倒退——实测许晴 内劲中期→内劲 被误报）。
                if parsed[0] < prev[0]:
                    alerts.append(RuleAlert(
                        level="block", rule_id="R-STATE", object_ref=name,
                        detail=(f"境界倒退：{prev} → {parsed}（{str(new_realm)}，at {at[0]}:{at[1]}）。"
                                "若无「跌境/自废修为」情节则为编纂错误")))
                elif parsed[0] == prev[0] and parsed[1] and prev[1] and parsed[1] < prev[1]:
                    alerts.append(RuleAlert(
                        level="block", rule_id="R-STATE", object_ref=name,
                        detail=(f"境界倒退：{prev} → {parsed}（{str(new_realm)}，at {at[0]}:{at[1]}）。"
                                "若无「跌境/自废修为」情节则为编纂错误")))
                elif parsed[0] - prev[0] >= 2:
                    alerts.append(RuleAlert(
                        level="warn", rule_id="R-STATE", object_ref=name,
                        detail=(f"越级跳变：跨 {parsed[0] - prev[0]} 个大境界（at {at[0]}:{at[1]}），"
                                "须有对应突破/机缘描写")))
            prev = parsed if prev is None else max(prev, parsed)

        if death_ch and cur.get("dead"):
            for ch_name, text in chapters.items():
                m = re.match(r"(\d+)-(\d+)$", ch_name)
                if not m:
                    continue
                at = (int(m.group(1)), int(m.group(2)))
                if at > death_ch and name in text:
                    alerts.append(RuleAlert(
                        level="warn", rule_id="R-STATE", object_ref=f"{ch_name}｜{name}",
                        detail=(f"人物已在 {death_ch[0]}:{death_ch[1]} 阵亡，"
                                "之后章节正文仍出现其名（确认是否回忆/提及）")))
    return alerts


def run_state_checks(ws: Workspace, project_id: str) -> list[RuleAlert]:
    """只跑世界状态规则（R-STATE），供编纂流程单独调用。"""
    return _worldstate_check(ws, project_id)


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
