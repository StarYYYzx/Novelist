"""蓝图体检（只读，2026-09-19 拍板：档 1 agent 化——build 后自动跑）。

为什么需要：固定模板构建出的蓝图有系统性盲区——今早真机里"主角金手指被写成
世界观铁律"一路穿过 seed/构建/审核，人和审校都没拦住。体检 agent 用**全局视野**
专查这类错位与失衡，是"构建正确性"的第二双眼睛。

两层混合：

1. **确定性预检**（零 LLM，`precheck`）：重名人物、泛型/实名主角卡并存、空描述伏笔、
   伏笔规模失衡、规模失控——不需要模型判断的直接扫蓝图。
2. **LLM 证据环**（ADR-032 `run_evidence` + 只读工具表）：预检信号 + 蓝图摘要作 goal，
   agent 可用只读工具继续取证；provider 无 `tool_calling` 时自动退化为单次问答
   （goal 里已带摘要，仍有产出）。

**只读铁律**：doctor 不写蓝图、不写 bible；唯一落盘是报告
`workspace/forge/doctor.md` 与 transcript 事件。任何失败返回 None——软失败，
绝不阻断 build。
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..core.output import emit
from ..storage.workspace import Workspace
from .state import Blueprint

DOCTOR_REL = "workspace/forge/doctor.md"

# 规模 sanity 上限：超出即报（一句话测试书被 LLM 自由发挥成 6 卷×100 章的教训）
_SCALE_CHAPTERS_WARN = 200
# 伏笔数量失衡线（09-16 事故里 threads 曾 7→18 失控）
_THREADS_WARN = 12


def precheck(bp: Blueprint) -> list[dict]:
    """确定性预检（纯函数）：返回 [{level, module, issue, advice}]。"""
    out: list[dict] = []

    chars = bp.section("characters") or []
    by_name: dict[str, list[str]] = {}
    for c in chars:
        name = str(c.get("name") or "").strip()
        if name:
            by_name.setdefault(name, []).append(str(c.get("id") or ""))
    for name, ids in sorted(by_name.items()):
        if len(ids) > 1:
            out.append({"level": "block", "module": "characters",
                        "issue": f"角色「{name}」有 {len(ids)} 张卡并存：{'、'.join(ids)}",
                        "advice": "保留实名卡、删除泛型/重复卡（forge craft 手改或 blueprint 编辑）"})
    # 泛型主角卡与实名主角卡并存（char:protagonist 是占位骨架，不该进正文 cast）
    generic = [c for c in chars if c.get("id") == "char:protagonist"]
    named = [c for c in chars if c.get("role") == "protagonist"
             or (c.get("name") and c.get("id") != "char:protagonist"
                 and any(c.get("name") == g.get("name") for g in generic))]
    if generic and any(c.get("id") != "char:protagonist" for c in named):
        out.append({"level": "warn", "module": "characters",
                    "issue": "泛型主角卡 char:protagonist 与实名卡并存（cast 可能抽到无境界的空卡）",
                    "advice": "删除泛型卡，或在构建前合并"})

    threads = bp.section("threads") or []
    # 真机形态（2026-09-19）：thread_set 给每个角色自动起一条伏笔，desc 有字但 name 空、
    # status 全 unplanned——空名伏笔进注入层是纯噪声
    empty = [t.get("id") for t in threads
             if not str(t.get("desc") or "").strip() or not str(t.get("name") or "").strip()]
    if empty:
        out.append({"level": "warn", "module": "threads",
                    "issue": f"{len(empty)} 条伏笔缺描述或名字：{'、'.join(str(x) for x in empty[:6])}",
                    "advice": "补描述/命名或删除（空壳伏笔进正文注入是噪声）"})
    unplanned = [t for t in threads if t.get("status") in (None, "unplanned")]
    if len(threads) > _THREADS_WARN and len(unplanned) == len(threads):
        out.append({"level": "warn", "module": "threads",
                    "issue": f"{len(threads)} 条伏笔全部 unplanned（疑似一角色一条的自动膨胀）",
                        "advice": "规划主线伏笔优先级，删除纯角色复读的条目"})

    meta = bp.get("meta") or {}
    scale = meta.get("scale") or {}
    try:
        total = int(scale.get("volumes") or 0) * int(scale.get("chapters_per_volume") or 0)
    except (TypeError, ValueError):
        total = 0
    if total > _SCALE_CHAPTERS_WARN:
        out.append({"level": "warn", "module": "meta",
                    "issue": f"规模 {scale.get('volumes')} 卷 × {scale.get('chapters_per_volume')} 章"
                             f" = {total} 章（测试/试水书建议收敛）",
                    "advice": "用 forge craft 调整 meta.scale，或确认这是真长篇规划"})

    missing_level = [c.get("name") for c in chars
                     if not ((c.get("power") or {}).get("level"))]
    if missing_level:
        out.append({"level": "warn", "module": "characters",
                    "issue": f"{len(missing_level)} 张人物卡缺境界/实力档："
                             f"{'、'.join(str(x) for x in missing_level[:6])}",
                    "advice": "境界是战力一致性锚点，补 power.level"})

    volumes = bp.section("volumes") or []
    for v in volumes:
        arc = v.get("arc") or {}
        if isinstance(arc, dict) and not (arc.get("goal") and arc.get("outcome")):
            out.append({"level": "warn", "module": "volumes",
                        "issue": f"第 {v.get('vol')} 卷 arc 缺 goal/outcome（承上启下断档）",
                        "advice": "roll/重生成该卷卷纲时补 arc 四要素"})

    out.extend(_line_checks(bp, scale))
    return out


def _line_checks(bp: Blueprint, scale: dict) -> list[dict]:
    """线账规划态守卫（2026-09-19 穿珠子落地；运行态欠账由 bead_view.warns 报）。"""
    res: list[dict] = []
    lines = bp.section("lines") or []
    threads = bp.section("threads") or []
    if not lines:
        return res
    line_ids = {str(x.get("id") or "") for x in lines}

    # ① 主线缺失 / 不唯一（一硬多警中的"硬"）
    mains = [x for x in lines if x.get("kind") == "main"]
    if not mains:
        res.append({"level": "block", "module": "lines",
                    "issue": "线索骨架缺主线（kind=main 恰好 1 条）",
                    "advice": "补登主线或重跑 book 节点——没有主线 bead_view 无法计算主线珠"})
    elif len(mains) > 1:
        res.append({"level": "block", "module": "lines",
                    "issue": f"主线不唯一：{'、'.join(str(x.get('id')) for x in mains)}",
                    "advice": "保留一条（/conflicts 裁决），其余转 subplot"})

    # ② 空壳线卡：缺 carrier（载体六类之一）——空壳进注入层是噪声
    hollow = [x.get("id") for x in lines if not str(x.get("carrier") or "").strip()]
    if hollow:
        res.append({"level": "warn", "module": "lines",
                    "issue": f"{len(hollow)} 条线缺载体（carrier 空）：{'、'.join(str(x) for x in hollow[:6])}",
                    "advice": "carrier 六类选一：物/行动/人/情感/组织/主题"})

    # ③ 支线区间：缺 planned_span 或区间非法/超全书规模
    try:
        cps = int(scale.get("chapters_per_volume") or 0)
    except (TypeError, ValueError):
        cps = 0
    for x in lines:
        if x.get("kind") != "subplot":
            continue
        sp = x.get("planned_span")
        if not isinstance(sp, dict) or not sp.get("start_ch") or not sp.get("end_ch"):
            res.append({"level": "warn", "module": "lines",
                        "issue": f"{x.get('id')}（支线）缺 planned_span——'不同长度的支线'无从表达",
                        "advice": "登记 {vol, start_ch, end_ch}，或明确该线为全书贯穿（转 main/hidden）"})
            continue
        b = int(sp["end_ch"])
        # 倒挂区间在登记时已被 _norm_span 归 None，此处不可能出现 a > b
        if cps and b > cps:
            res.append({"level": "warn", "module": "lines",
                        "issue": f"{x.get('id')} planned_span 终点 {b} 超出卷章数 {cps}",
                        "advice": "跨卷支线请拆分登记，或修正卷章规模"})

    # ④ 暗线缺 reveal_points：到不了"计划露头"，bead_view 永远不点名
    for x in lines:
        if x.get("kind") == "hidden" and not (x.get("reveal_points") or []):
            res.append({"level": "warn", "module": "lines",
                        "issue": f"{x.get('id')}（暗线）缺 reveal_points——无人知道它何时露头",
                        "advice": "登记计划揭示章位 [{vol, ch, note}]"})

    # ⑤ 伏笔 parent_line 悬空：指向不存在的线 id（真机 09-19 形态的反向）
    dangling = [t.get("id") for t in threads
                if str(t.get("parent_line") or "") and str(t.get("parent_line")) not in line_ids]
    if dangling:
        res.append({"level": "warn", "module": "threads",
                    "issue": f"{len(dangling)} 条伏笔 parent_line 悬空：{'、'.join(str(x) for x in dangling[:6])}",
                    "advice": "改指向存在的 ln: id，或删除 parent_line"})
    return res


_DOCTOR_SYSTEM = """你是长篇网文的蓝图体检医生。给你一本书的蓝图摘要与预检信号，你要找出
**错位**（东西放错了地方，如主角独有金手指被写成世界通用法则）、**失衡**
（比例/密度/节奏失控）、**缺口**（关键设定缺失）。只读，不修改任何文件。
可以用只读工具查阅 bible 文件取证。输出 JSON：
{"findings": [{"level": "block|warn", "module": "worldview|characters|threads|volumes|style|meta",
"issue": "问题一句话", "advice": "处置建议一句话"}]}
没有新问题就返回 {"findings": []}。只输出 JSON。"""


def _blueprint_digest(bp: Blueprint) -> str:
    """给 agent 的蓝图摘要（LLM 视图的起点；细节让它用工具自查）。"""
    meta = bp.get("meta") or {}
    wv = bp.get("worldview") or {}
    ps = wv.get("power_system") or {}
    lines = [
        f"书名：{meta.get('title')}；类型：{meta.get('genre')}；规模：{json.dumps(meta.get('scale') or {}, ensure_ascii=False)}",
        f"logline：{meta.get('logline')}",
        f"结局走向：{meta.get('endgame') or '（未设）'}",
        f"世界：{wv.get('name') or '（未名）'}；机制：{str(ps.get('mechanic') or '')[:120]}",
        f"境界表：{'、'.join(str(x) for x in (ps.get('levels') or [])) or '（无）'}",
        f"世界铁律 {len(wv.get('rules') or [])} 条：",
        *[f"  - {str(r)[:80]}" for r in (wv.get("rules") or [])[:8]],
        f"人物 {len(bp.section('characters') or [])} 张；伏笔 {len(bp.section('threads') or [])} 条；"
        f"卷规划 {len(bp.section('volumes') or [])} 卷；章细纲 {len(bp.section('chapters') or [])} 章",
    ]
    protag = next((c for c in (bp.section("characters") or [])
                   if c.get("role") == "protagonist" or c.get("id") == "char:protagonist"), None)
    if protag:
        lines.append(f"主角卡：{json.dumps({k: protag.get(k) for k in ('id', 'name', 'background', 'power', 'arc') if protag.get(k)}, ensure_ascii=False)[:400]}")
    return "\n".join(lines)


def run_doctor(ws: Workspace, project_id: str, bp: Blueprint, provider, *,
               log=emit, max_rounds: int = 6) -> dict | None:
    """体检主入口：确定性预检 + LLM 证据环。失败返回 None（软失败，不阻断构建）。"""
    pre = precheck(bp)
    agent_text = ""
    findings: list[dict] = []
    usage: dict[str, Any] = {}
    if provider is not None:
        try:
            from ..core.agent_runner import AgentRunner
            from ..core.session import Budget, SessionInfo
            from ..tools import evidence_registry

            runner = AgentRunner(
                provider,
                SessionInfo(project_id=project_id, agent="forge-doctor",
                            permission_profile="supervised"),
                budget=Budget(max_tokens_out=3000, max_rounds=max_rounds),
                registry=evidence_registry(ws),
            )
            goal = ("体检这本书的蓝图。\n\n【蓝图摘要】\n" + _blueprint_digest(bp)
                    + "\n\n【确定性预检信号】\n"
                    + (json.dumps(pre, ensure_ascii=False, indent=1) if pre else "（无）")
                    + "\n\n预检已报的不要重复；用你的全局视野找**新的**错位/失衡/缺口。"
                      "可以用只读工具查 bible 原文取证（如 bible/worldview.json、"
                      "bible/characters.json）。")
            run = runner.run_evidence(goal, system_prompt=_DOCTOR_SYSTEM,
                                      max_rounds=max_rounds, response_format="json_object")
            agent_text = run.final or ""
            usage = run.usage
            from ..core.normalize import loads_json_tolerant

            data = loads_json_tolerant(agent_text) or {}
            for f in (data.get("findings") or []):
                if isinstance(f, dict) and f.get("issue"):
                    findings.append({"level": str(f.get("level") or "warn"),
                                     "module": str(f.get("module") or "meta"),
                                     "issue": str(f["issue"])[:200],
                                     "advice": str(f.get("advice") or "")[:200]})
        except Exception as e:  # noqa: BLE001 - 体检失败绝不阻断构建
            log(f"[doctor] LLM 体检失败（仅保留确定性预检）：{type(e).__name__}: {e}")
    report = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
              "precheck": pre, "findings": findings, "usage": usage}
    _write_report(ws, project_id, report)
    from .state import append_transcript

    append_transcript(ws, project_id, "doctor.done", precheck=len(pre),
                      findings=len(findings),
                      tokens_in=usage.get("tokens_in"), tokens_out=usage.get("tokens_out"))
    n_block = sum(1 for f in pre + findings if f.get("level") == "block")
    log(f"[doctor] 体检完成：预检 {len(pre)} 项 + agent {len(findings)} 项"
        + (f"（含 block {n_block}）" if n_block else "")
        + "——报告 workspace/forge/doctor.md")
    for f in (pre + findings)[:6]:
        log(f"  [{f.get('level')}] {f.get('module')}: {f.get('issue')}")
    return report


def _write_report(ws: Workspace, project_id: str, report: dict) -> None:
    lines = ["# 蓝图体检报告", "",
             f"- 时间：{report['at']}",
             f"- 确定性预检：{len(report['precheck'])} 项；agent 发现：{len(report['findings'])} 项",
             ""]
    for group, title in (("precheck", "确定性预检"), ("findings", "Agent 发现")):
        lines.append(f"## {title}")
        lines.append("")
        items = report[group]
        if not items:
            lines.append("（无）")
        for f in items:
            lines.append(f"- **[{f['level']}] `{f['module']}`** {f['issue']}"
                         + (f" —— 建议：{f['advice']}" if f.get("advice") else ""))
        lines.append("")
    ws.write_text(ws._abs(f"{project_id}/{DOCTOR_REL}"), "\n".join(lines))  # noqa: SLF001
