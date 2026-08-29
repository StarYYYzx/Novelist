"""主编剧编排流（docs/04 §4.1 / docs/05 §2，M1 落地）。

正文严格串行逐章：编排器发起 → 主编剧经 AgentRunner 循环调用工具
（write_draft 等）→ 事件落点实时回写（commit_event）→ 一章完成。
"""

from __future__ import annotations

from .session import Budget, SessionInfo
from .writeback import LandedEvent, commit_event
from ..tools import build_registry


class ProductionResult:
    def __init__(self, ok: bool, chapter_path: str | None = None, result: str = "", events_committed: int = 0):
        self.ok = ok
        self.chapter_path = chapter_path
        self.result = result
        self.events_committed = events_committed


def produce_chapter(
    ws,
    project_id: str,
    vol: int,
    ch: int,
    provider,
    *,
    session: SessionInfo | None = None,
    system_prompt: str = "你是主编剧，负责指挥创作。",
    goal_prefix: str = "请撰写并落盘下一章草稿",
    max_rounds: int = 30,
) -> ProductionResult:
    """主编剧驱动产出第 vol 卷 ch 章草稿（串行）。

    - 绑定工具注册表（write_draft/write_file 等）。
    - AgentRunner 循环让 LLM 生成动作；遇到 write_draft 即落盘草稿。
    - 写完后触发一次"事件回写"（把本章作为一个事件落点实时固化，ADR-013）。
    """
    from .agent_runner import AgentRunner

    sess = session or SessionInfo(project_id=project_id, agent="orchestrator")
    reg = build_registry(ws)
    runner = AgentRunner(provider, sess, budget=Budget(max_tokens_out=2000, max_rounds=max_rounds), registry=reg)
    runner.system(system_prompt)
    goal = f"{goal_prefix}：第 {vol} 卷第 {ch} 章（project={project_id}）"

    try:
        final = runner.run_loop(goal, max_rounds=max_rounds)
    except Exception as e:  # noqa: BLE001 - 循环/工具/脚本异常统一收敛为失败
        return ProductionResult(ok=False, result=str(e))

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

    draft = ws.draft_path(project_id, vol, ch)
    return ProductionResult(ok=True, chapter_path=str(draft), result=final, events_committed=events)
