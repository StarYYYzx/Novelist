"""ADR-032 F1：chronicler 记忆编纂的证据仲裁环。

设计：**保留既有精调的 `extract()`**（状态/境界/时间/线索/视角都有针对性 prompt 与闸门），
另加一个**只读证据仲裁环路**（AgentRunner + `EVIDENCE_READ_TOOLS`）——LLM 在落库前
先用 `query_memory`/`get_character_history` 取到既有记忆与人物近况作为证据，再对每个
候选事件给出裁决：保持 / 忽略（与既有事件近似重复或矛盾）/ 改写（消歧后保留）/ 提请人工。

关键约束（拍板·只建议不直写）：本模块**不写任何库**。裁决结果只是对候选集的
“过滤 + 改写 + 标记”，真正落库仍由 `Chronicler.commit` 的确定性闸门（_gate_location /
_gate_near_dup / _gate_state）+ `MemoryWriter` 背书；`flagged` 项由编排层视情接
`ApprovalQueue` 提请人工。打成换取证方式，不是“谁有权落库”。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .agent_runner import AgentRunner
from .chronicler import Chronicler, ChroniclerReport, ExtractedEvent, Extraction

_VERDICT_RE = re.compile(
    r"^\s*(?:[-*•]|\d+[.、)．]?)?\s*(保持|忽略|改写|提请|确认)\s*[：:]\s*(\[?\d+\]?)\s*(?:[→-]\s*(.*?))?\s*(?:｜\s*(.*))?$",
    re.MULTILINE,
)


@dataclass
class ArbitrationResult:
    """一次证据仲裁的裁决与证据轨迹（ADR-032 F1）。

    - `kept`   : (候选序号, 最终摘要) 落库候选（改写后 / 保持原文）
    - `ignored`: (序号, 原摘要, 理由) 仲裁不落库（与既有事件近似重复/矛盾）
    - `flagged`: (序号, 摘要, 理由) 提请人工复核（证据不足/牵扯 covenant），编排层决定是否挂审批
    - `evidence`: 证据循环轨迹（sub-agent 读了什么），供人工复查
    """

    kept: list[tuple[int, str]] = field(default_factory=list)
    ignored: list[tuple[int, str, str]] = field(default_factory=list)
    flagged: list[tuple[int, str, str]] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    rounds: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.kept)


_ARBITRATOR_SYSTEM = """你是记忆层冲突仲裁员。系统刚用记忆编纂员从一章正文抽出了候选剧情事件。
落库前，你必须先取证：用工具查询既有记忆，判断每个候选是否与已入库事件**高度重复**或**明显矛盾**。

证据来源：
- query_memory —— 检索与候选摘要语义相近的既有剧情/经历
- get_character_history —— 读取某角色已入库的经历摘要

必须做的检查：
- 近似重复：既有的"击败 B" vs 候选"打败了 B"——要找出并忽略，避免记忆层噪声堆积
- 直接矛盾：候选称 A 已死/已失，但人物状态显示其仍存活——忽略候选，或改写为不冲突的表述

裁决输出（**严格按行**，每行给一个候选的裁决，候选用编号 [N]，最多给每个候选 1 行）：
- 保持：[N]
- 忽略：[N]｜理由（与既有事件近似重复 / 与人物状态矛盾 …）
- 改写：[N]-新摘要｜理由
- 提请：[N]｜理由（证据不足，需人工判断）

纪律：
- 无法用工具读到确凿证据时，倾向"保持"（宁漏勿误杀）；只有找到明确证据才忽略/改写
- 不要输出工具调用以外的解释；最终裁决必须是最上面那几种行之一
"""  # noqa: E501


def _parse_verdicts(content: str) -> tuple[dict[int, str], dict[int, str], dict[int, str]]:
    """解析裁决行 → (保持/改写摘要, 忽略理由, 提请理由)。

    优先级：忽略 > 提请 > 改写 > 保持；同一候选多次出现以后者覆盖前者但互斥。
    返回三张 dict：{候选序号: 落库摘要}、{序号: 忽略理由}、{序号: 提请理由}。
    """
    keep: dict[int, str] = {}
    ign: dict[int, str] = {}
    flag: dict[int, str] = {}
    for m in _VERDICT_RE.finditer(content or ""):
        act = m.group(1)
        try:
            idx = int(m.group(2).strip("[]"))  # 兼容 [1] / 1 双形态
        except (TypeError, ValueError):
            continue
        rest = (m.group(4) or "").strip()
        if act in ("忽略", "确认"):  # 「确认」= 确认应忽略的去重
            ign[idx] = rest or "仲裁忽略"
        elif act == "提请":
            flag[idx] = rest or "提请人工"
        elif act == "改写":
            keep[idx] = (m.group(3) or "").strip()
        else:  # 保持
            keep[idx] = m.group(3) or ""
    return keep, ign, flag


def arbitrate(
    chronicler: Chronicler,
    extraction: Extraction,
    chapter_text: str,
    *,
    provider,
    session,
    embedding=None,
    max_rounds: int = 6,
) -> ArbitrationResult:
    """对抽取候选跑只读证据仲裁环（ADR-032 F1）。无 LLM / 无候选 → 空 $kept`（不落库由上层回落）。

    代理只读证据 registry（`EVIDENCE_READ_TOOLS`）——**无任何写库工具**，
    结构上保证“仲裁不落库”。裁决解析失败/模型不配合时返回空裁决，由调用方回退到原候选。
    """
    out = ArbitrationResult()
    if not extraction.events:
        return out
    candidates: list[tuple[int, str]] = list(enumerate(extraction.events, 1))
    prompt = (
        "候选事件（编号与摘要）：\n"
        + "\n".join(f"[{i}] {ev.summary}" for i, ev in candidates)
        + "\n\n正文窗口：\n" + _window(chapter_text)
        + "\n\n请先取证（query_memory / get_character_history），再逐行给出裁决。"
    )

    from ..tools import evidence_registry

    try:
        runner = AgentRunner(provider, session, registry=evidence_registry(chronicler.ws, embedding),
                             max_rounds=max_rounds, thinking=True)
        run = runner.run_evidence(prompt, system_prompt=_ARBITRATOR_SYSTEM,
                                  response_format="text")
    except Exception:  # noqa: BLE001 - 仲裁失败降级：调用方回落到原候选集
        return out
    out.rounds = run.rounds
    out.evidence = run.evidence

    keep, ign, flag = _parse_verdicts(run.final)
    for i, ev in candidates:
        if i in ign:
            out.ignored.append((i, ev.summary, ign[i]))
        elif i in flag:
            out.flagged.append((i, ev.summary, flag[i]))
        else:
            out.kept.append((i, keep.get(i) or ev.summary))
    return out


def agentic_chronicle(
    chronicler: Chronicler,
    chapter_text: str,
    *,
    provider,
    session,
    embedding=None,
    vol: int = 1,
    ch: int = 1,
    max_events: int = 3,
    max_rounds: int = 6,
    tag: str = "c",
    payoff: bool = False,
) -> tuple[ChroniclerReport, ArbitrationResult]:
    """证据仲裁后的编纂入口（ADR-032 F1）：抽取 → 只读仲裁 → 确定性闸门落库。

    返回 `(report, arbitration)`。仲裁失败/无裁决时整体回退为原 `chronicler.run()`
    语义（新路径绝不比旧路径更差才沿用）。真正的写入始终走 `Chronicler.commit` 的
    确定性闸门——**仲裁只改候选集，不写库**。
    """
    from .chronicler import ChroniclerReport

    base = chronicler.extract(chapter_text, max_events=max_events)
    arb = arbitrate(chronicler, base, chapter_text, provider=provider,
                    session=session, embedding=embedding, max_rounds=max_rounds)
    event_pool = _settle_pool(base, arb)
    report = chronicler.commit(
        event_pool, vol, ch,
        state_changes=base.state_changes, tag=tag, payoff=payoff,
        time_lines=base.time_lines, pending_lines=base.pending_lines,
        line_rows=base.line_rows,
    )
    return report, arb


def _settle_pool(base: Extraction, arb: ArbitrationResult) -> list[ExtractedEvent]:
    """由仲裁结果定本轮待提交候选（保持/改写）；仲裁无裁决时回退原候选集。"""
    originals = {idx: ev for idx, ev in enumerate(base.events, 1)}
    if arb.kept:
        pool: list[ExtractedEvent] = []
        for idx, summary in arb.kept:
            orig = originals.get(idx)
            if orig is None:
                continue
            pool.append(ExtractedEvent(summary or orig.summary, orig.kind,
                                       list(orig.participant_ids), list(orig.threads)))
        return pool
    # 仲裁没产出可用裁决：不丢候选，回退原全集（宁多勿失，交由确定性闸门兜底）
    return list(base.events)


def _window(text: str, max_chars: int = 2500) -> str:
    """章窗口（头尾集中）：供仲裁环作为正文证据。"""
    return text[-max_chars:] if len(text) > max_chars else text


# 便捷：挂在 Chronicler 上的入口（供 produce_chapter 章级/事件级调用）
def run_agentic(chronicler: Chronicler, chapter_text, *, vol=1, ch=1, max_events=3,
                tag="c", payoff=False, provider=None, session=None, embedding=None,
                max_rounds=6):
    """`Chronicler.run_agentic` 落地：包装 `agentic_chronicle`。"""
    return agentic_chronicle(chronicler, chapter_text, provider=provider, session=session,
                             embedding=embedding, vol=vol, ch=ch, max_events=max_events,
                             max_rounds=max_rounds, tag=tag, payoff=payoff)