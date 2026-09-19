"""一致性确定性规则引擎（docs/04 §5.4 / docs/07 §5，ADR-009 规则层）。

输入：项目工作区的 bible/memory/outline 数据。
输出：结构化告警 `[{level, rule_id, object_ref, detail}]`（level=block|warn）。

当前规则：
- R-REF  引用完整性：entities 引用的 id 必须存在（characters/locations/plot_threads 互引）。
- R-TL   时间线单调：正文出现的时间线事件在 timeline 中有记录且顺序不矛盾。
- R-LEX  用词纪律：正文中出现现代词、西方典故、style.json 禁用词（原完全不扫正文）。
- R-PWR  战力体系表述一致：同一境界不得混用「层」「重」「级」等不同细分说法。
- R-CAST 出场覆盖度：建档人物在已写正文中零出场 → warn（docs/05「检查员」职能；
  端到端实测 22 建档人物 3 人零出场无告警）。区分「计划出场已越过但跳票」（强信号）
  与「无 first_appear 无法判断」（弱信号）；first_appear 在未来（渐进写作，卷 2+
  人物未在卷 1 出场）不告警。
- R-TITLE 称谓一致性（2026-09-04）：只查**视角无关的至高职位**（掌门/宗主/家主等
  ——不论谁开口称呼都一样），师承/师兄妹类**视角相对称谓刻意不查**（"自己的师姐
  在大师兄口中是师妹"——任一合法视角能解释即放行，用户拍板）。查两类：
  ①bible 内部：同一至高职位被 ≥2 角色声称 → warn；②正文 vs bible：正文以职位
  称呼了不属于该职位的人（「掌门段无涯」而 bible 掌门是青云子）→ warn。
  豁免（伪装剧情等）写 bible/title_rules.json：{"exempt": [{"name","title","note"}]}。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from ..storage.workspace import Workspace

# 现代词汇：出现在古风/修仙语境里即出戏（端到端实测「厚眼镜」）
MODERN_WORDS = (
    "眼镜", "手机", "电脑", "电话", "汽车", "飞机", "高铁", "咖啡", "超市", "公司",
    "经理", "化学", "物理", "电池", "网络", "视频", "打卡", "电梯",
    "医院", "警察", "总统", "沙发", "巧克力", "麦克风", "发动机",
)

# 现代喻体词（v7 真机实测 ch4「像指甲刮过黑板」）：穿越/现代背景文里
# 「手机/电脑」可作前世指称合理（worldview.modern_words 常显式设空放弃默认表），
# 但**作叙事明喻/修辞出现必出戏**——本表独立常开，不受 modern_words 覆盖影响；
# 需要豁免写 worldview.modern_words_exempt（同现代词）。
MODERN_SIMILES = (
    "黑板", "键盘", "鼠标", "摄像头", "幻灯片", "投影仪", "投影",
    "狙击", "雷达", "像素", "充电", "电路",
)

# 西方典故：与中文修仙语境冲突（端到端实测「达摩克利斯之剑」）
WESTERN_ALLUSIONS = (
    "达摩克利斯", "阿喀琉斯", "斯巴达", "宙斯", "丘比特", "潘多拉", "特洛伊",
    "伊甸", "普罗米修斯", "西西弗", "俄狄浦斯", "浮士德",
)

# 境界细分后缀：同一境界混用即为表述不一致
_LEVEL_SUFFIX_RE = r"([一二三四五六七八九]?[层重]|初期|中期|后期|大圆满|巅峰)"

# 至高职位词（R-TITLE）：一个组织同职位只能一人；**视角无关**——不论谁称呼，
# 「掌门」都指向同一个人。师承/同门类视角相对称谓（师尊/师兄/徒儿…）刻意不进
# 本表：换个人开口称谓就变，确定性规则无法归因说话人，交给 LLM 语义检。
SUPREME_TITLES = (
    "掌门", "宗主", "家主", "城主", "岛主", "谷主", "殿主", "阁主",
    "会长", "帮主", "门主", "教主", "皇帝", "国主",
)


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
    st = _read_json(ws.bible_path(project_id, "style")) or {}
    words = st.get("forbidden_words") if isinstance(st, dict) else None
    return [w for w in (words or []) if isinstance(w, str) and w]


def _modern_words(ws: Workspace, project_id: str) -> tuple[str, ...]:
    """现代词表按类型可配：都市/现代背景里「电梯/手机/公司」都正常。

    默认词表是修仙/古风导向（实测「厚眼镜」出戏）；worldview.json 可给
    `modern_words` 覆盖（都市文传空列表，只留真正出戏的词）。
    """
    wv = _read_json(ws.bible_path(project_id, "worldview")) or {}
    if isinstance(wv, dict) and isinstance(wv.get("modern_words"), list):
        return tuple(str(x) for x in wv["modern_words"])
    return MODERN_WORDS


def _modern_words_exempt(ws: Workspace, project_id: str) -> tuple[str, ...]:
    """现代词豁免表（v5 实测 P1-1）：穿越文里「前世作为程序员的直觉」语境合理。

    命中豁免词的现代词不再告警；worldview.json 的 `modern_words_exempt` 声明。
    """
    wv = _read_json(ws.bible_path(project_id, "worldview")) or {}
    if isinstance(wv, dict) and isinstance(wv.get("modern_words_exempt"), list):
        return tuple(str(x) for x in wv["modern_words_exempt"])
    return ()


def _modern_similes(ws: Workspace, project_id: str) -> tuple[str, ...]:
    """现代喻体词表：worldview.modern_similes 可覆盖，缺省用 MODERN_SIMILES。

    与 `modern_words`（指称型，穿越文可显式设空）不同——喻体表是修辞防线，
    worldview.modern_words 为空不关闭本表（v7 实测 ch4「指甲刮过黑板」）。
    """
    wv = _read_json(ws.bible_path(project_id, "worldview")) or {}
    if isinstance(wv, dict) and isinstance(wv.get("modern_similes"), list):
        return tuple(str(x) for x in wv["modern_similes"])
    return MODERN_SIMILES


def _lexicon_check(ws: Workspace, project_id: str) -> list[RuleAlert]:
    """R-LEX：正文用词纪律（现代词 / 现代喻体 / 西方典故 / style.json 禁用词）。"""
    alerts: list[RuleAlert] = []
    banned = _banned_words(ws, project_id)
    modern = _modern_words(ws, project_id)
    exempt = _modern_words_exempt(ws, project_id)
    modern_active = [w for w in modern if w not in exempt]
    similes = [w for w in _modern_similes(ws, project_id) if w not in exempt]
    for name, text in _iter_chapters(ws, project_id):
        for w in modern_active:
            if w in text:
                i = text.index(w)
                alerts.append(RuleAlert(
                    level="block", rule_id="R-LEX", object_ref=name,
                    detail=f"现代词汇「{w}」出戏：…{text[max(0, i - 10):i + len(w) + 8]}…"))
        for w in similes:
            if w in text:
                i = text.index(w)
                alerts.append(RuleAlert(
                    level="block", rule_id="R-LEX", object_ref=name,
                    detail=f"现代喻体「{w}」出戏（叙事修辞用了现代物）："
                           f"…{text[max(0, i - 10):i + len(w) + 8]}…"))
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
    wv = _read_json(ws.bible_path(project_id, "worldview")) or {}
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
    st = _read_json(ws.bible_path(project_id, "worldstate")) or {}
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


def _title_check(ws: Workspace, project_id: str) -> list[RuleAlert]:
    """R-TITLE 称谓一致性（至高职位，视角无关子集）。

    职位来源：角色卡 role 字段 + relationships[].type 文本（如「青云宗掌门」）。
    - ①bible 内部矛盾：同一至高职位被 ≥2 角色声称 → warn
    - ②正文 vs bible：正文出现「职位+他人名」/「他人名+职位」且该人不含此职位 → warn
      （正文全拼相连检查，"掌门段无涯"“段无涯掌门”均命中；称呼持有者本人不报）
    豁免名单 bible/title_rules.json：{"exempt": [{"name","title","note"}]}（伪装剧情）。
    """
    chars = _load(ws, project_id, "bible/characters.json")
    if not isinstance(chars, list):
        return []
    name_to_id: dict[str, str] = {}
    for c in chars:
        if isinstance(c, dict) and c.get("id"):
            if c.get("name"):
                name_to_id[str(c["name"])] = str(c["id"])
            for a in (c.get("aliases") or []):
                if a:
                    name_to_id[str(a)] = str(c["id"])

    def _claimed(c: dict) -> set[str]:
        """角色声称的至高职位——只认 role 字段（13 字段卡的角色/职位）。

        刻意**不**从 relationships[].type 提取：「对掌门怀恨在心」这类关系描述
        里的职位指的是对方或第三人，提取会把关系误当身份。
        """
        return {t for t in SUPREME_TITLES if t in str(c.get("role") or "")}

    claims: dict[str, set[str]] = {}   # title -> {角色名}
    who: dict[str, str] = {}           # title -> 首个声称者名
    for c in chars:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        for t in _claimed(c):
            claims.setdefault(t, set()).add(str(c["name"]))
            who.setdefault(t, str(c["name"]))

    exempt: set[tuple[str, str]] = set()
    tr = _load(ws, project_id, "bible/title_rules.json")
    for e in (tr.get("exempt") or []) if isinstance(tr, dict) else []:
        if isinstance(e, dict) and e.get("name") and e.get("title"):
            exempt.add((str(e["name"]), str(e["title"])))

    alerts: list[RuleAlert] = []
    # ① bible 内部：同一至高职位多人声称
    for t, names in claims.items():
        if len(names) > 1:
            alerts.append(RuleAlert(
                level="warn", rule_id="R-TITLE", object_ref=t,
                detail=f"bible 中「{t}」被多人声称：{sorted(names)}（一个组织同一至高"
                       "职位应只有一人；若是夺位/伪装剧情请写 title_rules.json 豁免）"))
    # ② 正文 vs bible：职位冠到别人头上（「掌门段无涯」/「段无涯掌门」紧邻模式）
    if claims:
        for _stem, text in _iter_chapters(ws, project_id):
            for t, owner in who.items():
                legit = claims.get(t, {owner})
                for n in name_to_id:
                    if n == owner or n in legit or (n, t) in exempt:
                        continue
                    if n not in text:
                        continue
                    # 名与职位**严格紧邻**才算称呼（「掌门段无涯」「段无涯掌门」）；
                    # 允许间隔会误伤「段无涯在掌门大选中…」这类叙述
                    if re.search(re.escape(t) + re.escape(n), text) or \
                       re.search(re.escape(n) + re.escape(t), text):
                        alerts.append(RuleAlert(
                            level="warn", rule_id="R-TITLE", object_ref=n,
                            detail=f"正文将「{n}」称为「{t}」，但 bible 中 {t} 是"
                                   f"「{owner}」。若为伪装/自称/错称剧情，"
                                   "写 bible/title_rules.json 豁免"))
                        break   # 每人每职位报一次即够
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
    """全部确定性规则（bible 引用 + 时间线 + 正文用词与战力表述 + 世界状态 + 出场覆盖）。

    注意：这里的"全部"也只是**能确定性判定**的部分。称谓是否合乎身份、
    情节逻辑是否自洽、伏笔是否回收等仍属 LLM 语义检（见 consistency/reviewer.py）。
    """
    alerts: list[RuleAlert] = []
    alerts += _referential_integrity(ws, project_id)
    alerts += _timeline_monotonic(ws, project_id)
    alerts += run_lexicon_checks(ws, project_id)
    alerts += run_state_checks(ws, project_id)
    alerts += _thread_payoff_check(ws, project_id)
    alerts += _cast_coverage_check(ws, project_id)
    alerts += _title_check(ws, project_id)
    return alerts


def _last_written_chapter(ws: Workspace, project_id: str) -> tuple[int, int] | None:
    """已写正文的最大章节号（chapters/ 或 drafts/chapters/，stem 形如 `1-12`）。"""
    best: tuple[int, int] | None = None
    for name, _text in _iter_chapters(ws, project_id):
        m = re.match(r"(\d+)-(\d+)", name)
        if not m:
            continue
        cur = (int(m.group(1)), int(m.group(2)))
        if best is None or cur > best:
            best = cur
    return best


def _cast_coverage_check(ws: Workspace, project_id: str) -> list[RuleAlert]:
    """R-CAST：建档人物出场覆盖度（docs/05「检查员」职能，端到端实测 22 建档 3 人零出场）。

    - 已写正文从未出现姓名/别名的人物 → warn。匹配按全名/别名子串，**单字名跳过**
      （正文随处单字，误报率高）；
    - 人物卡有 `first_appear {vol,ch}` 且已写章节已越过 → 强信号（计划出场却跳票）；
    - 无 `first_appear` → 弱信号（无法判断是否计划内，人工复核）；
    - `first_appear` 在未来（渐进写作：卷 2+ 人物未在卷 1 出场属正常）→ 不告警；
    - 已死亡/退场（status=deceased/dead/retired）不查——「出场覆盖」只约束活人；
    - 无任何正文 → 空（避免全人物噪音）。
    """
    alerts: list[RuleAlert] = []
    chapters = _iter_chapters(ws, project_id)
    if not chapters:
        return alerts
    full = "\n".join(text for _n, text in chapters)
    last = _last_written_chapter(ws, project_id)
    if not last:
        return alerts
    chars = _load(ws, project_id, "bible/characters.json")
    if not isinstance(chars, list):
        return alerts
    for c in chars:
        if not isinstance(c, dict):
            continue
        if str(c.get("status") or "") in ("deceased", "dead", "retired"):
            continue
        cid = c.get("id") or "?"
        name = str(c.get("name") or "").strip()
        if len(name) < 2:
            continue
        aliases = [str(a).strip() for a in (c.get("aliases") or []) if isinstance(a, str) and len(a.strip()) >= 2]
        appeared = name in full or any(a in full for a in aliases)
        if appeared:
            continue
        fa = c.get("first_appear")
        if isinstance(fa, dict) and isinstance(fa.get("vol"), int):
            fa_at = (int(fa["vol"]), int(fa.get("ch") or 1))
            if fa_at <= last:
                alerts.append(RuleAlert(
                    level="warn", rule_id="R-CAST", object_ref=cid,
                    detail=(f"人物「{name}」计划在 {fa_at[0]}:{fa_at[1]} 出场，"
                            f"已写到 {last[0]}:{last[1]} 仍零出场——细纲点名或 JIT 补卡遗漏，"
                            "或出场安排被跳过（确认是伏笔延迟还是漏写）")))
        elif isinstance(fa, dict):
            fa_at = (int(fa.get("vol") or 0), int(fa.get("ch") or 1))
            if fa_at > last:
                continue  # 计划在未来，渐进写作正常
            alerts.append(RuleAlert(
                level="warn", rule_id="R-CAST", object_ref=cid,
                detail=(f"人物「{name}」first_appear 格式异常（{fa}），无法判断是否该已出场")))
        else:
            alerts.append(RuleAlert(
                level="warn", rule_id="R-CAST", object_ref=cid,
                detail=(f"人物「{name}」已写到 {last[0]}:{last[1]} 仍零出场，"
                        "且人物卡未声明 first_appear——无法判断是否计划内，人工复核"
                        "（若为后续卷重要角色可补 first_appear 豁免）")))
    return alerts


def _thread_payoff_check(ws: Workspace, project_id: str) -> list[RuleAlert]:
    """R-THREAD：卷末伏笔回收检查（第九批·收尾工作流的确定性校验层）。

    - 收尾期（卷剩余章数 ≤ tail_chapters）仍 active/planted 的**卷内**伏笔 → warn
      （该开始兑现了）；
    - 卷末最后一章仍 active/planted 的卷内伏笔 → block（本卷一条都没收）。
    scope=="book"（全书主线跨卷）不检查——那不是本卷的责任。
    """
    from ..core.phase import Phase, PhasePolicy, VolumeContext, payoff_checklist

    alerts: list[RuleAlert] = []
    # 找出已写到的最后一章（R-THREAD 只对"已到收尾期"的卷做判定）
    chapters = _iter_chapters(ws, project_id)
    if not chapters:
        return alerts
    last_stem = chapters[-1][0]
    try:
        vol_s, ch_s = last_stem.split("-")[:2]
        vol, ch = int(vol_s), int(ch_s)
    except ValueError:  # pragma: no cover
        return alerts
    policy = PhasePolicy.load(ws, project_id)
    vctx = VolumeContext.load(ws, project_id, vol)
    phase, _reason = policy.judge(vctx, ch)
    if phase is not Phase.TAIL:
        return alerts
    is_final = vctx.end and ch >= vctx.end
    checklist = payoff_checklist(ws, project_id, vol, ch)
    for t in checklist.get("threads") or []:
        tid = t.get("id") or "?"
        if is_final:
            alerts.append(RuleAlert(
                level="block", rule_id="R-THREAD", object_ref=tid,
                detail=f"卷末最后一章，卷内伏笔「{t.get('desc', '')[:30]}」仍为 "
                       f"{t.get('status')}——本卷暗线未回收（收尾工作流要求最后一章给出交代）"))
        else:
            alerts.append(RuleAlert(
                level="warn", rule_id="R-THREAD", object_ref=tid,
                detail=f"卷剩余 {vctx.chapters_left(ch)} 章，伏笔「{t.get('desc', '')[:30]}」"
                       f"仍为 {t.get('status')}——收尾期该开始兑现（回收清单已注入生成提示）"))
    return alerts


def _worldstate_check(ws: Workspace, project_id: str) -> list[RuleAlert]:
    """R-STATE：世界状态层的确定性校验（人工审查第三批第 3 条）。

    - 境界只进不退（倒退 → block；"跌境/自废修为"类情节当前不做例外，出现时人工仲裁）
    - 一次跨 2 个及以上大境界 → warn（须有对应突破/机缘描写，否则是编纂员抽错了）
    - 已死亡人物在死亡章之后仍出现在正文 → warn（可能是回忆/提及，人工复核）
    """
    from ..core.worldstate import parse_realm

    alerts: list[RuleAlert] = []
    wv = _read_json(ws.bible_path(project_id, "worldview")) or {}
    levels = ((wv.get("power_system") or {}).get("levels")) if isinstance(wv, dict) else None
    # 绑定流角色豁免（v5 实测 P0-1 完整版）：realm 随绑定对象波动是**机制本身**，
    # 编纂不是每次都带「借用/同步」标记——worldview.realm_fluctuates 列出的角色
    # （按 id 或名字）跳过单调性比较，只保留死亡检查。
    fluctuate: set[str] = set()
    if isinstance(wv, dict) and isinstance(wv.get("realm_fluctuates"), list):
        fluctuate = {str(x) for x in wv["realm_fluctuates"]}
    st = _read_json(ws.bible_path(project_id, "worldstate")) or {}
    chars = st.get("characters") if isinstance(st, dict) else None
    if not isinstance(chars, dict):
        return alerts

    chapters = {name: text for name, text in _iter_chapters(ws, project_id)}

    # 基线：init_from_bible 时的初始修为快照（worldstate.baselines）。
    # ADR-013 完全体（2026-09-06）之后角色卡 power.level 会被事件级实然同步，
    # 不再适合做单调性起点——旧项目无 baselines 时 fallback 人物卡（向后兼容）。
    card_realms: dict[str, str] = {}
    card_data = _read_json(ws.bible_path(project_id, "characters")) or []
    for c in card_data if isinstance(card_data, list) else []:
        if isinstance(c, dict) and c.get("id") and (c.get("power") or {}).get("level"):
            card_realms[c["id"]] = str(c["power"]["level"])
    baselines: dict[str, str] = {}
    if isinstance(st.get("baselines"), dict):
        baselines = {str(k): str(v) for k, v in st["baselines"].items()
                     if v}  # type: ignore[union-attr]

    for cid, cur in chars.items():
        if not isinstance(cur, dict):
            continue
        name = cur.get("name") or cid
        if cid in fluctuate or name in fluctuate:
            continue  # 绑定流角色：境界波动是机制，不校验单调性
        seq = [h for h in (cur.get("history") or [])
               if isinstance(h, dict) and isinstance(h.get("at"), dict)]
        seq.sort(key=lambda h: (int(h["at"].get("vol", 0) or 0), int(h["at"].get("ch", 0) or 0)))

        # 初始修为作为单调性比较的基线：baselines 快照优先，fallback 人物卡
        _base_txt = baselines.get(cid) or card_realms.get(cid)
        prev: tuple[int, int] | None = None
        if levels and _base_txt:
            prev = parse_realm(_base_txt, [str(x) for x in levels])
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
            # 绑定流豁免（v5 实测 P0-1）：realm 含「借用/同步/临时/绑定」等标记时，
            # 修为是临时借用而非自身境界——跳过单调性比较，且不更新基线
            # （否则绑定值被当成真实修为，解绑回落会被误报成倒退）。
            realm_str = str(new_realm)
            if any(mark in realm_str for mark in ("借用", "同步", "临时", "绑定", "附体", "借调")):
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

        # 不可出场期（ADR-019）：闭关/失踪/昏迷/被囚/渡劫期间不得出场
        if cur.get("unavailable_until") is not None:
            since = cur.get("unavailable_since")
            if isinstance(since, dict):
                since_at = (int(since.get("vol", 0) or 0), int(since.get("ch", 0) or 0))
                for ch_name, text in chapters.items():
                    m = re.match(r"(\d+)-(\d+)$", ch_name)
                    if not m:
                        continue
                    at = (int(m.group(1)), int(m.group(2)))
                    if at > since_at and name in text:
                        alerts.append(RuleAlert(
                            level="warn", rule_id="R-STATE", object_ref=f"{ch_name}｜{name}",
                            detail=(f"人物正在{cur.get('unavailable_reason') or '闭关'}中"
                                    f"（不可出场期至 t+{cur['unavailable_until']}，自 "
                                    f"{since_at[0]}:{since_at[1]} 起），第 {at[0]}:{at[1]} 章仍出现"
                                    "其名（确认是否回忆/提及，或应推迟到出关后）")))
    return alerts


def run_state_checks(ws: Workspace, project_id: str) -> list[RuleAlert]:
    """只跑世界状态规则（R-STATE + R-TIME），供编纂流程单独调用。"""
    return _worldstate_check(ws, project_id) + _pending_time_check(ws, project_id)


def _pending_time_check(ws: Workspace, project_id: str) -> list[RuleAlert]:
    """R-TIME：定时事件（pending）到期状态（ADR-019，docs/06 §3.3.3）。

    - overdue ≥ 3 章 → warn（该兑现了）；
    - overdue ≥ 5 章 → warn 软 block（本章须体现，否则拦截）；
    - 已 expired → warn 留痕（自动作废，建议人工处理/改期）。
    """
    from ..core.timeline import now_of, pending_of

    alerts: list[RuleAlert] = []
    st = _load(ws, project_id, "bible/worldstate.json")
    if not isinstance(st, dict):
        return alerts
    now = now_of(st)
    for p in pending_of(st):
        what = str(p.get("what") or "")[:30]
        pid = str(p.get("id") or "?")
        status = p.get("status")
        if status == "expired":
            alerts.append(RuleAlert(
                level="warn", rule_id="R-TIME", object_ref=pid,
                detail=f"「{what}」已逾期自动作废（expired）——事件不再拦截，建议人工处理或改期"))
            continue
        if status != "scheduled":
            continue
        od = int(p.get("overdue") or 0)
        due = int(p.get("due") or 0)
        if od >= 5:
            bc = int(p.get("block_count") or 0)
            detail = (f"「{what}」due=t+{due}（now=t+{now}）已逾期 {od} 章未兑现，"
                      f"软 block {bc}/3——本章 key_events 须体现该事项"
                      if bc < 3 else
                      f"「{what}」已逾期 {od} 章，软 block {bc}/3——下一章将被自动作废")
            alerts.append(RuleAlert(level="warn", rule_id="R-TIME", object_ref=pid,
                                    detail=detail))
        elif od >= 3:
            alerts.append(RuleAlert(
                level="warn", rule_id="R-TIME", object_ref=pid,
                detail=f"「{what}」due=t+{due}（now=t+{now}）已到期 {od} 章未兑现，该安排兑现了"))
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
    """R-TL：时间线单调（docs/06 §3.3，ADR-019）。

    **按 `at.t`（相对天数）判单调**——天数是事实轴，(vol, ch) 只是粗粒度投影；
    旧数据（无 t）回退按 in_chapters 首现章节序判。两者都缺失才 warn。
    """
    alerts: list[RuleAlert] = []
    tls = _load(ws, project_id, "bible/timeline.json")
    if not isinstance(tls, list):
        return alerts
    prev: tuple[Any, int] | None = None   # (key, type)：key=t 或 (vol, ch)，type 0|1 区分
    prev_id = ""
    for t in tls:
        if not isinstance(t, dict):
            continue
        tid = str(t.get("id") or "?")
        at = t.get("at") if isinstance(t.get("at"), dict) else None
        t_key = None
        if isinstance(at, dict) and isinstance(at.get("t"), (int, float)):
            t_key = int(at["t"])
        if t_key is not None:
            cur: tuple[int, int] = (t_key, 0)
            if prev is not None and prev[1] == 0 and cur[0] < prev[0]:
                alerts.append(RuleAlert(
                    level="block", rule_id="R-TL", object_ref=tid,
                    detail=(f"时间线事件 {tid}（t={cur[0]}）早于前一事件 "
                            f"{prev_id}（t={prev[0]}）——时间倒退")))
            prev, prev_id = (t_key, 0), tid
            continue
        # 旧数据回退：按 in_chapters 首现章节序
        chaps = t.get("in_chapters") or []
        if chaps and isinstance(chaps[0], dict):
            cur_ch = (int(chaps[0].get("vol", 0)), int(chaps[0].get("ch", 0)))
            if prev is not None and prev[1] == 1 and cur_ch < prev[0]:
                alerts.append(RuleAlert(
                    level="block", rule_id="R-TL", object_ref=tid,
                    detail=f"时间线事件 {tid} 出现顺序({cur_ch[0]}:{cur_ch[1]}) 早于前一事件({prev[0][0]}:{prev[0][1]})"))
            prev, prev_id = (cur_ch, 1), tid
        else:
            alerts.append(RuleAlert(level="warn", rule_id="R-TL", object_ref=tid,
                                    detail="时间线事件缺少 at.t 与 in_chapters，无法判单调"))
    return alerts


def sort_alerts(alerts: list[RuleAlert]) -> list[RuleAlert]:
    import operator

    return sorted(alerts, key=operator.attrgetter("object_ref", "rule_id"))


def to_dict(alerts: list[RuleAlert]) -> list[dict]:
    return [{"level": a.level, "rule_id": a.rule_id, "object_ref": a.object_ref, "detail": a.detail} for a in alerts]
