"""细纲连读审查（批次二·方案 A，用户 2026-09-05 提议落地）。

核心理念（用户原话）："将整体的流程交给 AI 顺一遍"——细纲落盘后、写正文前，
把本卷已写全部细纲 + 蓝图事件账本**连读**一遍，让 LLM 以全局视野找：

1. 跨章母题重复（措辞不同、意象相同的相邻事件——机械账本的盲区）；
2. 事件因果链断裂 / 时间线矛盾；
3. 人物首登场排布合理性；
4. 节奏问题（连续同型事件等）。

产出：
- ``workspace/forge/coherence-v{vol}.json``（findings，生成侧读）
- ``reports/reviews/gist-coherence-v{vol}.md``（人读评审稿）

审查失败静默跳过（增强层，不阻断 build）；不挂 ADR-024 闸门——findings 直接
注入后续章 prompt 的【连读禁令】，人可在评审稿上复盘。
"""
from __future__ import annotations

import json
import re
import time

from ..core.llm import LLMMessage, LLMRequest

FINDINGS_REL = "workspace/forge/coherence-v{vol}.json"
REPORT_REL = "reports/reviews/gist-coherence-v{vol}.md"
BP_FINDINGS_REL = "workspace/forge/coherence-blueprint.json"
BP_REPORT_REL = "reports/reviews/gist-coherence-blueprint.md"

_BP_PROMPT = (
    "你是长篇小说的蓝图审稿人。以下是这本书的蓝图（世界观 / 核心人物 / 卷规划 / "
    "伏笔线索）。全书尚未展开任何正文——现在发现的自相矛盾会污染整个下游，"
    "请以**全局视野**审查并只输出 JSON（不要多余文字）：\n"
    '{"contradictions": [{"where": "世界观/人物/卷规划/伏笔", '
    '"issue": "具体的自相矛盾或逻辑断裂", "severity": "high|mid|low"}],\n'
    ' "character_conflicts": [{"who": "角色名", "issue": "撞型/动机冲突/关系矛盾"}],\n'
    ' "structure_notes": "卷结构/节奏层面的问题，无则空串",\n'
    ' "must_watch": ["给后续写作的具体注意事项（每条一句话，可执行）"]}\n'
    "注意：只报**真问题**（设定互斥、人物撞型、时间线断裂），"
    "不要把正常的设计张力当矛盾。must_watch 最多 6 条。\n\n")

_PROMPT_HEAD = (
    "你是长篇小说的连读审稿人。以下是同一卷前 N 章的细纲（章题 + 关键事件 + "
    "张力/钩子）。请以**全局视野**审查并只输出 JSON（不要多余文字）：\n"
    '{"motif_repeats": [{"motif": "重复的动作/意象母题", "chapters": [章号], '
    '"advice": "给后续章的具体禁令/替代建议"}],\n'
    ' "causal_issues": [{"chapters": [章号], "issue": "因果/时间线问题"}],\n'
    ' "first_appearance_issues": [{"ch": 章号, "who": "角色/组织", "issue": "首次出现未交代"}],\n'
    ' "notes": "其他节奏/结构问题，无则空串"}\n'
    "注意：motif_repeats 只收**措辞不同但意象相同**的跨章复用（如两章各自"
    "『手机推送新闻』『指尖轻划杀敌』），不要把剧情正常推进当重复。\n\n")


def gather_chapter_plans(ws, bp, vol: int, up_to_ch: int) -> list[dict]:
    """收集本卷 1..up_to_ch 章的细纲要点（蓝图 chapters 段为准）。"""
    plans = []
    for g in bp.section("chapters") or []:
        try:
            if int(g.get("vol") or 0) != vol:
                continue
            ch = int(g.get("ch") or 0)
        except (TypeError, ValueError):
            continue
        if not (1 <= ch <= up_to_ch):
            continue
        plans.append({
            "ch": ch,
            "title": g.get("title") or "",
            "key_events": [str(e) for e in (g.get("key_events") or [])],
        })
    plans.sort(key=lambda x: x["ch"])
    return plans


def run_coherence_review(ws, project_id: str, bp, provider, vol: int,
                         up_to_ch: int, *, count_hook=None) -> dict | None:
    """连读审查：一次 LLM 调用产出 findings 并落盘。失败返回 None。

    `count_hook`：每次 provider 调用后回调一次（G2 修复，2026-09-05）——
    审查调用计入 build/roll 的调用预算与 transcript，原先直接 complete
    绕过计量，成本不可见。
    """
    plans = gather_chapter_plans(ws, bp, vol, up_to_ch)
    if len(plans) < 2:
        return None  # 单章无可比性
    body = "\n".join(
        f"## 第 {p['ch']} 章 {p['title']}\n" +
        "\n".join(f"- {e}" for e in p["key_events"])
        for p in plans)
    prompt = _PROMPT_HEAD + body
    try:
        res = provider.complete(LLMRequest(
            messages=[LLMMessage(role="user", content=prompt)],
            max_tokens_out=1500, temperature=0.3))
        if count_hook is not None:
            try:
                count_hook()
            except Exception:  # noqa: BLE001 - 计量钩子失败不影响审查
                pass
        raw = (res.content or "").strip() if getattr(res, "ok", False) else ""
    except Exception:  # noqa: BLE001
        return None
    findings = _parse_findings(raw)
    if findings is None:
        return None
    findings["vol"] = vol
    findings["up_to_ch"] = up_to_ch
    findings["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _persist(ws, project_id, vol, findings, plans)
    return findings


def _parse_findings(raw: str) -> dict | None:
    """宽容解析 LLM 输出 JSON（容忍 ```json 围栏与前后缀文字）。"""
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    for key in ("motif_repeats", "causal_issues", "first_appearance_issues"):
        if not isinstance(data.get(key), list):
            data[key] = []
    data.setdefault("notes", "")
    return data


def _persist(ws, project_id: str, vol: int, findings: dict, plans: list[dict]) -> None:
    try:
        ws.write_json(ws._abs(project_id + "/" + FINDINGS_REL.format(vol=vol)),  # noqa: SLF001
                      findings)
        lines = [f"# 细纲连读审查 第 {vol} 卷（至第 {findings.get('up_to_ch')} 章）",
                 f"- 时间：{findings.get('at')}", ""]
        for mr in findings.get("motif_repeats") or []:
            lines.append(f"## 母题重复：{mr.get('motif')}（第 "
                         f"{'、'.join(str(c) for c in mr.get('chapters') or [])} 章）")
            lines.append(f"- 建议：{mr.get('advice')}")
        for ci in findings.get("causal_issues") or []:
            lines.append(f"## 因果问题（第 {'、'.join(str(c) for c in ci.get('chapters') or [])} 章）")
            lines.append(f"- {ci.get('issue')}")
        for fi in findings.get("first_appearance_issues") or []:
            lines.append(f"## 首登场（第 {fi.get('ch')} 章 {fi.get('who')}）：{fi.get('issue')}")
        if findings.get("notes"):
            lines.append(f"## 其他\n- {findings['notes']}")
        if len(lines) <= 3:
            lines.append("（未发现问题）")
        ws.write_text(ws._abs(project_id + "/" + REPORT_REL.format(vol=vol)),  # noqa: SLF001
                      "\n".join(lines) + "\n")
    except Exception:  # noqa: BLE001 - 落盘失败不阻断
        pass


def load_coherence_bans(ws, project_id: str, vol: int, next_ch: int) -> list[str]:
    """生成侧读取：适用于 next_ch 的禁令行（母题建议 + 首登场提醒）。"""
    try:
        p = ws._abs(project_id + "/" + FINDINGS_REL.format(vol=vol))  # noqa: SLF001
        if not p.exists():
            return []
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    for mr in data.get("motif_repeats") or []:
        chs = [int(c) for c in (mr.get("chapters") or []) if str(c).isdigit()]
        if chs and next_ch in chs:
            continue  # 重复发生在本章自身，禁令已无意义
        if mr.get("advice"):
            out.append(str(mr["advice"]))
    for fi in data.get("first_appearance_issues") or []:
        try:
            if int(fi.get("ch") or 0) == next_ch and fi.get("who"):
                out.append(f"本章「{fi['who']}」首次出现必须交代身份/与主角关系")
        except (TypeError, ValueError):
            continue
    return out[:8]


def summarize_for_prompt(bans: list[str]) -> str:
    """禁令行 → prompt 块文本（空列表返回空串）。"""
    if not bans:
        return ""
    return "【连读审查禁令】以下问题在细纲连读审查中被发现，本章生成必须遵守：\n" + \
        "\n".join(f"- {b}" for b in bans)


# ---------------------------------------------------------------- 蓝图连读（批次三·方案1）

def run_blueprint_review(ws, project_id: str, bp, provider, *, count_hook=None) -> dict | None:
    """蓝图完成后、卷展开前的 LLM 连读审查（批次三·方案1）。

    蓝图一次产出全部核心设定与卷规划——这里的自相矛盾/人物撞型会污染整个下游，
    是"交给 AI 顺一遍"价值最高的引入点。一次调用产出 findings 并落盘；
    失败静默返回 None（增强层，不阻断 build）。
    `count_hook`：调用计数钩子（G2，同 run_coherence_review）。
    """
    try:
        wv = bp.section("worldview") or {}
        chars = bp.section("characters") or []
        vols = bp.section("volumes") or []
        threads = bp.section("threads") or []
    except Exception:  # noqa: BLE001
        return None
    if not (vols or chars):
        return None  # 空蓝图无可审

    def _seg(title: str, data) -> str:
        return f"## {title}\n" + json.dumps(data, ensure_ascii=False, indent=1)[:3500] + "\n"

    body = (_seg("世界观", wv) + _seg("核心人物", chars) +
            _seg("卷规划", vols) + _seg("伏笔线索", threads))
    prompt = _BP_PROMPT + body
    try:
        res = provider.complete(LLMRequest(
            messages=[LLMMessage(role="user", content=prompt)],
            max_tokens_out=1500, temperature=0.3))
        if count_hook is not None:
            try:
                count_hook()
            except Exception:  # noqa: BLE001 - 计量钩子失败不影响审查
                pass
        raw = (res.content or "").strip() if getattr(res, "ok", False) else ""
    except Exception:  # noqa: BLE001
        return None
    findings = _parse_bp_findings(raw)
    if findings is None:
        return None
    findings["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _persist_blueprint(ws, project_id, findings)
    return findings


def _parse_bp_findings(raw: str) -> dict | None:
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    for key in ("contradictions", "character_conflicts", "must_watch"):
        if not isinstance(data.get(key), list):
            data[key] = []
    data.setdefault("structure_notes", "")
    return data


def _persist_blueprint(ws, project_id: str, findings: dict) -> None:
    try:
        ws.write_json(ws._abs(project_id + "/" + BP_FINDINGS_REL),  # noqa: SLF001
                      findings)
        lines = [f"# 蓝图连读审查（{findings.get('at')}）", ""]
        for c in findings.get("contradictions") or []:
            lines.append(f"## 矛盾[{c.get('severity', '?')}]（{c.get('where')}）："
                         f"{c.get('issue')}")
        for c in findings.get("character_conflicts") or []:
            lines.append(f"## 人物撞型（{c.get('who')}）：{c.get('issue')}")
        if findings.get("structure_notes"):
            lines.append(f"## 结构\n- {findings['structure_notes']}")
        for w in findings.get("must_watch") or []:
            lines.append(f"- [注意] {w}")
        if len(lines) <= 2:
            lines.append("（未发现问题）")
        ws.write_text(ws._abs(project_id + "/" + BP_REPORT_REL),  # noqa: SLF001
                      "\n".join(lines) + "\n")
    except Exception:  # noqa: BLE001 - 落盘失败不阻断
        pass


def load_blueprint_bans(ws, project_id: str) -> list[str]:
    """生成侧读取：蓝图审查的 must_watch + 高危矛盾 → 禁令/注意事项行。"""
    try:
        p = ws._abs(project_id + "/" + BP_FINDINGS_REL)  # noqa: SLF001
        if not p.exists():
            return []
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    for c in data.get("contradictions") or []:
        if str(c.get("severity") or "").lower() == "high" and c.get("issue"):
            out.append(f"[蓝图矛盾] {c['issue']}")
    for c in data.get("character_conflicts") or []:
        if c.get("who") and c.get("issue"):
            out.append(f"[人物撞型-{c['who']}] {c['issue']}")
    out += [str(w) for w in (data.get("must_watch") or []) if w]
    return out[:8]
