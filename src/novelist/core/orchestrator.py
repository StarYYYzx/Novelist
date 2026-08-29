"""主编剧编排流（docs/04 §4.1 / docs/05 §2，M1 落地）。

正文严格串行逐章：编排器发起 → 主编剧经 AgentRunner 循环调用工具
（write_draft 等）→ 事件落点实时回写（commit_event）→ 一章完成。

本地/慢 token 模型降级（ADR-006）：若模型直接返回正文文本而非工具调用
（本地小模型 tool_calling 常不可靠），把返回文本直接落盘为草稿，跳过工具往返，
以"生成一次、落盘一次"减少多轮 LLM 调用，量力而为（docs/07 §8 降级总表）。
"""

from __future__ import annotations

from .session import Budget, SessionInfo
from .writeback import LandedEvent, commit_event
from ..tools import build_registry


class ProductionResult:
    def __init__(self, ok: bool, chapter_path: str | None = None, result: str = "", events_committed: int = 0,
                 mode: str = "tool"):
        self.ok = ok
        self.chapter_path = chapter_path
        self.result = result
        self.events_committed = events_committed
        self.mode = mode  # "tool"（工具调用写草稿） | "direct"（文本直接落盘）


def produce_chapter(
    ws,
    project_id: str,
    vol: int,
    ch: int,
    provider,
    *,
    session: SessionInfo | None = None,
    registry=None,
    system_prompt: str = "你是主编剧，负责指挥创作。",
    goal_prefix: str = "请撰写并输出下一章正文。",
    max_rounds: int = 30,
    direct_words_floor: int = 20,
    prefer_direct: bool = False,
    generation_tokens: int = 4000,
) -> ProductionResult:
    """主编剧驱动产出第 vol 卷 ch 章草稿（串行）。

    - 绑定工具注册表（write_draft/write_file 等）。
    - prefer_direct=False：优先 AgentRunner 工具循环（write_draft 落盘）。
    - prefer_direct=True：直接一次生成正文文本并落盘——**适合本地/慢 token 模型**
      （ADR-006 文本型降级），避免多轮工具调用的往返开销。
    - generation_tokens：单次生成预算；本地慢模型（约 20 token/s）请给较小值
      （如 600≈30s），以免超时。
    - 无论哪种模式，写完后触发一次"事件回写"（ADR-013）。
    """
    from .agent_runner import AgentRunner
    from ..core.llm import LLMMessage, LLMRequest

    sess = session or SessionInfo(project_id=project_id, agent="orchestrator")
    goal = f"{goal_prefix}：第 {vol} 卷第 {ch} 章（project={project_id}）"

    mode = "tool"
    try:
        if prefer_direct:
            res = provider.complete(
                LLMRequest(
                    messages=[LLMMessage(role="system", content=system_prompt),
                              LLMMessage(role="user", content=goal)],
                    max_tokens_out=generation_tokens,
                )
            )
            final = res.content or ""
            if len(final) < direct_words_floor:
                raise RuntimeError(f"direct generation too short ({len(final)} chars)")
            mode = "direct"
        else:
            reg = registry or build_registry(ws)
            runner = AgentRunner(provider, sess, budget=Budget(max_tokens_out=generation_tokens, max_rounds=max_rounds), registry=reg)
            runner.system(system_prompt)
            final = runner.run_loop(goal, max_rounds=max_rounds)
            # 若循环返回的是足够长的最终文本但未触发 write_draft -> 文本落盘
            if final and len(final) >= direct_words_floor:
                mode = "direct"
    except Exception as e:  # noqa: BLE001 - 循环/生成异常统一收敛为失败
        return ProductionResult(ok=False, result=str(e))

    # 落盘草稿：
    # - direct 模式：模型直接返回正文，落盘 final 文本。
    # - tool 模式：write_draft 工具已落盘正文，此处不覆盖（保持工具产物）。
    draft = ws.draft_path(project_id, vol, ch)
    if mode == "direct":
        try:
            draft.parent.mkdir(parents=True, exist_ok=True)
            ws.write_text(draft, final + "\n")
        except Exception:  # noqa: BLE001
            return ProductionResult(ok=False, result="cannot write draft")


    # 事件回写：把"本章产生"这一事件实时固化（docs/05 §5.4 第 1-3 步）
    ev = LandedEvent(
        project_id=project_id, vol=vol, ch=ch, seq=1,
        kind="chapter", summary=f"完成第 {vol} 卷第 {ch} 章",
    )
    try:
        commit_event(ev, sess)
        events = 1
    except Exception:  # noqa: BLE001 - 回写失败不影响本书草稿已落盘
        events = 0

    return ProductionResult(ok=True, chapter_path=str(draft), result=final, events_committed=events, mode=mode)
