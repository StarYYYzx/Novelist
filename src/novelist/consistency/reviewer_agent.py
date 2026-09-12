"""ADR-032 F2：审校师 Agent 化——初审后「定向取证再定论」。

现状 `Reviewer.review` 是一次性 LLM 泛读：对照圣经/前情出工单，但模型**没法**在写
工单前针对可疑点去查记忆核实——只能凭注入的摘要一次下结论，可疑点措辞含糊就跳过或误报。

本模块用 ADR-032 只读证据环扩展它：审校员在拿不定时可调用证据工具
（`query_memory`/`get_character_history`/`read_file` 读 bible/记忆/前文）核实，再把
确有把握的问题收敛成工单输出。

契约不变：最终仍输出 `level | category | detail | suggestion` 问题行（与 REVIEW_PROMPT
一致），由 `Reviewer._parse` 解析——下游（编排层/`review` 命令）零改动。只读、只出工单、
不直写；敏感/存疑项可挂 `ApprovalQueue` 由编排层决定是否提请人工。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.agent_runner import AgentRunner
from ..core.session import SessionInfo
from .reviewer import REVIEW_PROMPT, ReviewIssue  # noqa: F401  (REVIEW_PROMPT 复用检索上下文)

REVIEWER_AGENT_SYSTEM = """你是审校师。对照设定圣经、前情与细纲，审读本章/本片段正文并找问题。

【取证纪律】先通读证据（下述设定圣经摘要/前情/细纲），对拿不准的可疑点，用工具去查证：
- query_memory —— 查某人物/某事实在既有记忆里的真实记载（核实前情、已死/已毁等矛盾）
- get_character_history —— 读某角色已入库的经历，核对其言行是否漂移
- read_file —— 可读 bible/记忆/前文原文核实细节（路径在 prompt 内提供）

不要凭猜断定罪：查不到确凿证据的可疑点宁可不下结论；只有核实到明确矛盾才报。

【输出】核实完毕后，只输出问题行，每行严格为：级别 | 维度 | 问题描述 | 修订建议
- 级别：block（必须改）/ warn（建议改）
- 维度仅限：设定矛盾 / 人设漂移 / 称谓失当 / 时间线 / 战力越级 / 事实前后矛盾 / 细纲未覆盖 / 伏笔 / 其他
- 同一问题最多报一行（去重合并）；没有问题则只输出一行：ok
- 不要输出解释、序号或分隔线
"""


@dataclass
class AgenticReview:
    issues: list[ReviewIssue] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    rounds: int = 0
    mode: str = "agent"  # agent | fallback


def agentic_review(
    text: str,
    vol: int,
    ch: int,
    *,
    ws,
    project_id: str,
    provider,
    session: SessionInfo,
    embedding=None,
    gist_text: str = "",
    memories: list[str] | None = None,
    cast_ids: list[str] | None = None,
    scope: str = "",
    max_rounds: int = 8,
) -> AgenticReview:
    """只读证据环审校（ADR-032 F2）。

    复用 `REVIEW_PROMPT` 的圣经/前情/细纲/范围上下文（与单发审校同一输入口径），
    但允许取证后出工单。失败/无问题 → `mode="fallback"` 回退单发 `Reviewer.review`，
    绝不比旧路径更差。
    """
    from ..tools import evidence_registry
    from .reviewer import Reviewer, _bible_brief

    out = AgenticReview()
    if not text.strip() or provider is None:
        return out
    reviewer = Reviewer(ws, project_id, llm=provider)
    prompt = REVIEW_PROMPT.format(
        bible=_bible_brief(ws, project_id, cast_ids),
        memory="\n".join(memories or []) or "（无前情）",
        gist=gist_text[:800] or "（无细纲）",
        scope=scope or "【范围说明】本次审读的是完整一章。",
    ) + text[-3000:]
    # 蓝图里给出可读的 bible/记忆产物路径，供左撇子取证（read_file）
    ctx = prompt + _evidence_pointer(ws, project_id)

    reg = evidence_registry(ws, embedding=embedding)
    runner = AgentRunner(provider, session, registry=reg, max_rounds=max_rounds,
                         thinking=True)
    try:
        run = runner.run_evidence(ctx, system_prompt=REVIEWER_AGENT_SYSTEM,
                                  response_format="text")
    except Exception:  # noqa: BLE001 - 证据环失败回退单发
        return AgenticReview(issues=reviewer.review(text, vol, ch, gist_text=gist_text,
                                                    memories=memories, cast_ids=cast_ids,
                                                    scope=scope),
                             mode="fallback")
    out.rounds = run.rounds
    out.evidence = run.evidence
    if run.final and run.final.strip().lower() != "ok":
        out.issues = Reviewer._parse(reviewer, run.final)
    # final 为 ok/空 → 无问题，issues 保持空
    return out


def _evidence_pointer(ws, project_id: str) -> str:
    """给证据环可读文件路径提示（bible 与记忆事实源）。"""
    prefix = ws._abs(project_id)
    rel = str(prefix)
    return (
        "\n\n【可取证文件（相对项目根）】"
        "\nbible/characters.json · bible/worldview.json · memory/plot_events.json"
        f"\n（工作区根：{rel}；read_file 的 path 需以项目目录开头）"
    )