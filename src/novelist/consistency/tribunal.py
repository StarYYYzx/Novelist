"""三视角交叉编译判定器（DL-015 / ADR-038，2026-10-24 拍板）。

把「模型判断」提升为**编译级判定**的机制（用户拍板，两阶段管线·编译期核心）：

- **三视角**（参照系刻意不同，三角覆盖）：
  - `guardian` 设定守护者：对照 worldstate（实然）+ 世界观铁律（应然）+ 章级事实账本，
    抓正文与设定/实然的冲突 → **hard**（硬事实类，DL-006 新口径：机器/交叉可确证的错误）；
  - `reader` 读者：只看前文尾部 + 本章正文（**不带任何设定**），抓正文自身不自洽
    （逻辑矛盾/动机断裂/前后不一致）→ soft；
  - `editor` 编辑：对照细纲，抓叙事断裂与无据偏离 → soft。
- **证据引用机器复核**（拍板：报错必须附原文引用）：引用（去空白）必须是正文子串，
  对不上该条判定**无效**——防判定器自己幻觉。
- **多数决**（拍板）：同一问题 ≥2 视角报 = 编译错误；单视角 = 观察名单（攒批人工复盘，
  每卷一次）。hard/soft 由参与视角定：守护者参与（即事实/设定冲突）→ hard，
  仅读者+编辑 → soft。
- **修复循环**（拍板：分级处置）：hard 错误带证据重写本章 → 复检，≤`rewrite_max` 轮；
  修不过 → **阻断**（blocked，等人工——错误不静默放行）；soft 错误修不过 → 降级欠账，
  落 `reports/compile_debt.json`（终润前清偿，doctor 展示）。

成本：每章 3 次判定调用 + 复检均摊 ~1 次（flash 价位可忽略；正确性优先于成本，
用户 2026-10-24 拍板）。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from ..core.llm import LLMMessage, LLMRequest
from ..core.output import emit
from ..storage.workspace import Workspace

PERSPECTIVES = ("guardian", "reader", "editor")
DEBT_REL = "reports/compile_debt.json"

# 视角 → 严重度（确定性映射，拍板口径：守护者=事实/设定冲突=hard）
_PERSPECTIVE_SEVERITY = {"guardian": "hard", "reader": "soft", "editor": "soft"}

# 观察名单聚合进欠账时同一问题的去重键前缀（每卷人工复盘一次，拍板）
_MATCH_TOKEN_JACCARD = 0.35


@dataclass
class Issue:
    """一条经机器复核的判定（引用已验证为正文子串）。"""

    perspective: str
    type: str          # 问题类型短语（模型自拟）
    quote: str         # 正文原文引用
    basis: str         # 判定依据
    suggestion: str = ""
    severity: str = "soft"

    def to_dict(self) -> dict:
        return {"perspective": self.perspective, "type": self.type,
                "quote": self.quote[:120], "basis": self.basis[:160],
                "suggestion": self.suggestion[:160], "severity": self.severity}


@dataclass
class TribunalReport:
    """一轮三视角判定的聚合结果。"""

    hard: list[Issue] = field(default_factory=list)        # 确认的编译错误（含守护者）
    soft: list[Issue] = field(default_factory=list)        # 确认的软语义问题（读者+编辑）
    watchlist: list[Issue] = field(default_factory=list)   # 单视角（攒批人工复盘）
    invalid_quotes: int = 0                                # 被机器复核打回的引用数

    @property
    def ok(self) -> bool:
        return not self.hard


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _tokens(s: str) -> set[str]:
    # 中文按 2-gram 切 token（无空语），英文/数字按词
    s = re.sub(r"[\s，。；：、！？''\"（）\[\]【】|｜]", "", s or "")
    grams = {s[i:i + 2] for i in range(len(s) - 1)}
    words = set(re.findall(r"[A-Za-z0-9_]{2,}", s))
    return grams | words


def _issues_match(a: Issue, b: Issue) -> bool:
    """两条判定是否指向同一问题：引用包含，或 (类型+依据) token 重叠足够高。"""
    qa, qb = _norm(a.quote), _norm(b.quote)
    if qa and qb and (qa in qb or qb in qa):
        return True
    ja = _tokens(a.type + a.basis)
    jb = _tokens(b.type + a.basis)
    if not ja or not jb:
        return False
    return len(ja & jb) / len(ja | jb) >= _MATCH_TOKEN_JACCARD


def _cluster(issues: list[Issue]) -> list[list[Issue]]:
    """贪心聚类：同视角不合并，跨视角按 _issues_match 归组。"""
    groups: list[list[Issue]] = []
    for issue in issues:
        for g in groups:
            if issue.perspective not in {x.perspective for x in g} \
                    and any(_issues_match(issue, x) for x in g):
                g.append(issue)
                break
        else:
            groups.append([issue])
    return groups


def aggregate(issues: list[Issue]) -> TribunalReport:
    """多数决聚合（拍板）：≥2 视角 = 确认；单视角 = 观察名单。"""
    rep = TribunalReport()
    for group in _cluster(issues):
        if len({x.perspective for x in group}) >= 2:
            target = rep.hard if any(x.severity == "hard" for x in group) else rep.soft
            target.extend(group)
        else:
            rep.watchlist.extend(group)
    return rep


# ---------------------------------------------------------------- 上下文装配

def _read_json(ws: Workspace, pid: str, rel: str):
    p = ws._abs(f"{pid}/{rel}")  # noqa: SLF001
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_text(ws: Workspace, pid: str, rel: str) -> str:
    p = ws._abs(f"{pid}/{rel}")  # noqa: SLF001
    try:
        return p.read_text(encoding="utf-8") if p.exists() else ""
    except OSError:
        return ""


def _prev_tail(ws: Workspace, pid: str, vol: int, ch: int, chars: int = 1500) -> str:
    """前一章正文尾部（读者视角的"读者已读"窗口）。草稿优先（最新事实）。"""
    for rel in (f"drafts/chapters/{vol}-{ch - 1}.md", f"chapters/{vol}-{ch - 1}.md"):
        text = _read_text(ws, pid, rel)
        if text:
            return text[-chars:]
    return ""


def _guardian_context(ws: Workspace, pid: str) -> str:
    wst = _read_json(ws, pid, "bible/worldstate.json") or {}
    lines: list[str] = []
    chars = wst.get("characters") or {}
    for cid, c in list(chars.items())[:24]:
        if isinstance(c, dict):
            bits = [f"{cid} {c.get('name') or ''}",
                    f"境界={c.get('realm') or '—'}", f"位置={c.get('location') or '—'}",
                    f"状态={'死亡' if c.get('dead') else '存活'}"]
            lines.append("；".join(bits))
    # 2026-10-25（大章节 E2E）：守护者此前看不到人物卡——把主角金手指判成
    # "凭空引入设定外元素"（cheat 就在主角卡上）。补关键人物卡的 cheat/身份。
    try:
        cards = _read_json(ws, pid, "bible/characters.json") or []
        for c in cards[:16]:
            if not isinstance(c, dict):
                continue
            bits = [f"{c.get('id')} {c.get('name') or ''}"]
            if c.get("is_protagonist") or c.get("role") == "protagonist":
                bits.append("【主角】")
            pw = c.get("power") or {}
            if isinstance(pw, dict) and pw.get("level"):
                bits.append(f"境界={pw['level']}")
            if c.get("cheat"):
                bits.append(f"金手指（设定内，出现相关描写不算凭空引入）: {str(c['cheat'])[:80]}")
            if c.get("background"):
                bits.append(f"身份: {str(c['background'])[:50]}")
            lines.append("；".join(bits))
    except Exception:  # noqa: BLE001 - 人物卡读取失败不阻断判定
        pass
    wv = _read_json(ws, pid, "bible/worldview.json") or {}
    rules = wv.get("rules") or []
    ps = (wv.get("power_system") or {}) if isinstance(wv, dict) else {}
    if rules:
        lines.append("世界铁律：" + "；".join(str(r) for r in rules[:8]))
    if ps.get("levels"):
        lines.append("境界体系：" + "＜".join(str(x) for x in ps["levels"][:14]))
    facts = _read_json(ws, pid, "workspace/chapter_facts.json") or {}
    entries = facts.get("entries") or []
    recent = entries[-2:] if isinstance(entries, list) else []
    for e in recent:
        if isinstance(e, dict) and e.get("facts"):
            lines.append(f"第{e.get('vol')}卷{e.get('ch')}章事实：" +
                         "；".join(str(x) for x in e["facts"][:12]))
    return "\n".join(lines) if lines else "（无 worldstate/世界观/事实账本——仅按正文内部一致性判）"


# ---------------------------------------------------------------- 判定协议

_ISSUE_PROTOCOL = """只输出 JSON（不要解释、不要围栏），结构：
{"issues": [{"type": "问题类型短语（如：境界倒退/物品凭空出现/逻辑矛盾/动机断裂/偏离细纲）",
             "quote": "正文中体现该问题的原文片段（10-40 字，必须逐字摘自正文）",
             "basis": "判定依据一句话（对照什么、为何冲突）",
             "verdict": "issue（确认为问题，可省略）或 not_an_issue（复核后不成立）",
             "suggestion": "修法建议一句话"}]}
没有问题时输出 {"issues": []}。宁可少报不误报；每条必须能给 quote；
拿不准或复核后自相矛盾的条目给 verdict=not_an_issue，不要硬凑。"""


def _perspective_prompt(perspective: str, ws: Workspace, pid: str,
                        vol: int, ch: int, text: str) -> str:
    if perspective == "guardian":
        return f"""你是设定守护者。对照下方的实然状态与设定，找出本章正文中与它们**冲突**的地方
（境界/状态/位置/生死/物品/铁律/已确立事实——读者会说"写错了"的事实性错误）。

【实然状态与设定（判定的唯一依据）】
{_guardian_context(ws, pid)}

【本章正文（第{vol}卷第{ch}章）】
{text}

{_ISSUE_PROTOCOL}"""
    if perspective == "reader":
        return f"""你是一位挑剔的读者。你**只能看到下面给你的正文**（看不到任何设定），
找出正文**自身**不自洽的地方：前后矛盾、逻辑不通、人物动机断裂、
前文说过的后文忘了、同一场景描述冲突。

【上一章结尾（你已读过的部分）】
{_prev_tail(ws, pid, vol, ch) or "（本书开篇）"}

【本章正文（第{vol}卷第{ch}章）】
{text}

{_ISSUE_PROTOCOL}"""
    return f"""你是责任编辑。对照细纲检查本章正文：叙事断裂、事件衔接缺失、
无理由偏离细纲安排（细纲允许合理细化，不允许丢事件/换因果）。

【本章细纲】
{_read_text(ws, pid, f"outline/chapters/{vol}-{ch}.md")[:2400] or "（无细纲——跳过偏离检查，只看叙事断裂）"}

【本章正文（第{vol}卷第{ch}章）】
{text}

{_ISSUE_PROTOCOL}"""


def _parse_issues(raw: str, perspective: str, chapter_text: str) -> tuple[list[Issue], int]:
    """解析一轮判定输出 + 引用机器复核（对不上即无效，防判定器幻觉）。"""
    text = raw.strip()
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return [], 0
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return [], 0
    body = _norm(chapter_text)
    issues: list[Issue] = []
    invalid = 0
    for row in (data.get("issues") or [])[:12]:
        if not isinstance(row, dict):
            continue
        quote = str(row.get("quote") or "").strip()
        if not quote or _norm(quote) not in body:
            invalid += 1          # 引用复核失败：判定无效（ADR-038 拍板）
            continue
        # 2026-10-25（大章节 E2E）：判定器自查后认为不成立的条目不再输出为
        # hard——真机出现过"此条仅核验，不成立，应删除"仍被计成 hard 的噪声。
        verdict = str(row.get("verdict") or "").strip()
        basis0 = str(row.get("basis") or "")
        if verdict in ("not_an_issue", "不成立", "false_positive")                 or ("不成立" in basis0 and len(basis0) < 60):
            continue
        issues.append(Issue(
            perspective=perspective,
            type=str(row.get("type") or "未分类")[:40],
            quote=quote,
            basis=str(row.get("basis") or "")[:200],
            suggestion=str(row.get("suggestion") or "")[:200],
            severity=_PERSPECTIVE_SEVERITY[perspective],
        ))
    return issues, invalid


def run_tribunal(ws: Workspace, project_id: str, vol: int, ch: int,
                 chapter_text: str, provider) -> TribunalReport:
    """一轮三视角判定：3 次 LLM 调用 + 引用机器复核 + 多数决聚合。"""
    from ..core.prompt_budget import head_tail_window

    windowed = head_tail_window(chapter_text, 8000)
    issues: list[Issue] = []
    invalid = 0
    for p in PERSPECTIVES:
        res = provider.complete(LLMRequest(
            messages=[LLMMessage(role="user",
                                 content=_perspective_prompt(p, ws, project_id, vol, ch, windowed))],
            # 2026-10-25 盘点：1600+思考=真机 7 次全 0 字（三视角判定层实际未工作过），
            # 预算 3000；仍空/截断时关思考重试一次（多数决容忍缺票，但 0 字=三票全缺）
            temperature=0.2, max_tokens_out=3000, response_format="json_object",
            thinking=True,  # 判断类：一致性判定，开思考
        ))
        if not (getattr(res, "content", "") or "").strip()                 and getattr(res, "finish_reason", "") == "length":
            res = provider.complete(LLMRequest(
                messages=[LLMMessage(role="user",
                                     content=_perspective_prompt(p, ws, project_id, vol, ch, windowed))],
                temperature=0.2, max_tokens_out=3000, response_format="json_object",
                thinking=False))
        if getattr(res, "blocked", False) or not (res.content or "").strip():
            continue  # 单视角失败不炸整轮（多数决天然容忍缺一票）
        got, bad = _parse_issues(res.content, p, chapter_text)
        issues.extend(got)
        invalid += bad
    rep = aggregate(issues)
    rep.invalid_quotes = invalid
    return rep


# ---------------------------------------------------------------- 修复循环

def rewrite_with_verdicts(text: str, hard: list[Issue], provider,
                          *, vol: int = 0, ch: int = 0) -> str:
    """带判定证据重写整章（修复循环的"修"）。修订稿明显劣化（<60% 原文长）则弃用。"""
    lines = ["你是小说正文修订器。下面这章被编译检查判定了若干**硬错误**，",
             f"请逐条修正（第{vol}卷第{ch}章），保留未出问题的情节、人物与对白，",
             "输出修正后的**完整正文**（不要解释、不要节选）：\n"]
    for i, issue in enumerate(hard, 1):
        lines.append(f"{i}. [{issue.type}] 正文「{issue.quote}」——{issue.basis}"
                     + (f"（建议：{issue.suggestion}）" if issue.suggestion else ""))
    lines.append(f"\n【原文】\n{text}")
    res = provider.complete(LLMRequest(
        messages=[LLMMessage(role="user", content="\n".join(lines))],
        temperature=0.3, max_tokens_out=8000, thinking=False))
    if getattr(res, "blocked", False):
        return text
    out = (res.content or "").strip()
    if len(out) < max(400, int(len(text) * 0.6)):
        return text  # 修订稿异常短：视为修订失败，保留原文交给复检/阻断
    return out


def _load_craft_rules(ws: Workspace, project_id: str) -> list[dict]:
    """读本书实例化工艺规则（bible/style.json 的 craft_rules，DL-015⑦）。"""
    p = ws.bible_path(project_id, "style")   # D-8 棘轮：路径访问器族是唯一正道
    try:
        st = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        st = {}
    rules = st.get("craft_rules") if isinstance(st, dict) else None
    return [r for r in (rules or []) if isinstance(r, dict)]


def compile_pass(ws: Workspace, project_id: str, vol: int, ch: int,
                 text: str, provider, *, rewrite_max: int = 2) -> tuple[str, dict]:
    """编译闸门：机检规则层（零 LLM）+ 三视角判定 → 修复循环（hard 修不过阻断 /
    soft 降级欠账）。

    返回 `(文本, 报告dict)`——报告恒为 dict（含 blocked/hard/soft/watchlist/rounds），
    调用方（编排层）把它并进 ProductionResult；判定器自身异常由调用方降级跳过。
    """
    from ..craft import instantiate as _craft
    from ..craft.instantiate import eval_craft_checks

    craft_rules = _load_craft_rules(ws, project_id)
    # 本章出场角色（细纲 characters 字段）：机检 requires=protagonist 判定用
    _gist = _read_text(ws, project_id, f"outline/chapters/{vol}-{ch}.md")
    import re as _re

    _present_ids = _re.findall(r"char:[A-Za-z0-9_-]+", _gist)
    _cards = _read_json(ws, project_id, "bible/characters.json") or []
    _craft.set_presence_context([c for c in (_cards if isinstance(_cards, list) else [])
                                 if isinstance(c, dict)])
    rounds = 0
    report = TribunalReport()
    current = text
    while True:
        # 第一道闸：工艺规则机检（DL-015⑦，确定性零 LLM）——违例直接 hard
        # （机器可证），与三视角多数决互补：规则管可判定的，交叉判定管语义地带。
        report = run_tribunal(ws, project_id, vol, ch, current, provider)
        for v in eval_craft_checks(craft_rules, current, _present_ids):
            report.hard.append(Issue(
                perspective="rules", type=f"工艺规则违反（{v.get('check_id')}）",
                quote=str(v.get("quote") or "")[:120],
                basis=str(v.get("note") or ""), severity="hard"))
        rounds += 1
        emit(f"[compile] 第{rounds}轮：hard={len(report.hard)} soft={len(report.soft)} "
             f"观察名单={len(report.watchlist)}（无效引用 {report.invalid_quotes}）")
        if not report.hard or rounds > rewrite_max:
            break
        current = rewrite_with_verdicts(current, report.hard, provider, vol=vol, ch=ch)
    blocked = bool(report.hard)
    if blocked:
        emit(f"[compile] ✗ 硬错误修不掉（{len(report.hard)} 条）——本章阻断，等人工；"
             "欠账已记录，重跑本章将自动清理旧回写（reset_state）")
    _record_debt(ws, project_id, vol, ch, report, blocked=blocked)
    return current, {
        "blocked": blocked,
        "hard": [x.to_dict() for x in report.hard],
        "soft": [x.to_dict() for x in report.soft],
        "watchlist": [x.to_dict() for x in report.watchlist],
        "invalid_quotes": report.invalid_quotes,
        "rounds": rounds,
    }


# ---------------------------------------------------------------- 欠账清单

def _record_debt(ws: Workspace, project_id: str, vol: int, ch: int,
                 report: TribunalReport, *, blocked: bool) -> None:
    """软语义欠账 + 观察名单落 `reports/compile_debt.json`（doctor 展示，终润前清偿）。

    观察名单按用户拍板**每卷复盘一次**：文件按卷聚合，去重键 = 类型+引用首 40 字。
    """
    rows: list[dict] = [x.to_dict() for x in report.soft]
    rows += [x.to_dict() for x in report.watchlist]
    if blocked:
        rows += [dict(x.to_dict(), blocked=True) for x in report.hard]
    if not rows:
        return
    try:
        p = ws._abs(f"{project_id}/{DEBT_REL}")  # noqa: SLF001
        p.parent.mkdir(parents=True, exist_ok=True)
        data: dict = {}
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
        bucket = data.setdefault("by_volume", {}).setdefault(str(vol), [])
        seen = {str(r.get("type")) + _norm(str(r.get("quote"))[:40]) for r in bucket}
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        for r in rows:
            key = r["type"] + _norm(r["quote"][:40])
            if key in seen:
                continue
            seen.add(key)
            bucket.append(dict(r, vol=vol, ch=ch, at=now))
        data["updated_at"] = now
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        import os

        os.replace(tmp, p)
    except OSError:
        pass  # 欠账落盘失败不阻断（doctor 与终润能读到多少算多少）


def load_debt(ws: Workspace, project_id: str) -> dict:
    """读欠账清单（doctor / 终润清偿用）。"""
    data = _read_json(ws, project_id, DEBT_REL)
    return data if isinstance(data, dict) else {}


def clear_debt_for_chapter(ws: Workspace, project_id: str, vol: int, ch: int) -> int:
    """清掉某章的欠账（该章重写/人工处置后调用）。返回清除条数。"""
    data = load_debt(ws, project_id)
    bucket = (data.get("by_volume") or {}).get(str(vol)) or []
    keep = [r for r in bucket if int(r.get("ch") or 0) != ch]
    removed = len(bucket) - len(keep)
    if removed:
        try:
            data["by_volume"][str(vol)] = keep
            p = ws._abs(f"{project_id}/{DEBT_REL}")  # noqa: SLF001
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            import os

            os.replace(tmp, p)
        except OSError:
            return 0
    return removed
