"""会话与预算类型（docs/07 §3.2 契约）。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SessionInfo:
    """一次 Agent 派发的会话标识（docs/07 §3.2）。"""

    project_id: str
    agent: str  # orchestrator | sub:<name> | actor:<char_id>
    permission_profile: str = "supervised"
    actor_char_id: str | None = None  # 仅演员 Agent 有值（数据隔离依据）
    task_id: str | None = None  # 当前派发任务 id（审计关联）


@dataclass
class Budget:
    """预算/配额（docs/07 §3.2、docs/06 §7）。

    AG-12（2026-09-15 审计）：此前只有"单次请求"的 `max_tokens_out` 真正生效，
    `max_tokens_in`/`max_cost` 从不累计、`max_rounds` 之外没有任何闭环。现补累计量
    （由 `AgentRunner` 逐轮过账，供审计与证据轨迹使用）；`max_cost` 的**硬停**是可选项，
    默认关（拍板：先记账拿到真实用量，再定阈值）。
    """

    max_tokens_out: int
    max_tokens_in: int | None = None
    max_cost: float | None = None
    max_rounds: int | None = None  # 循环/围读会轮次上限
    # ---- 累计用量（运行时由 AgentRunner 写入，只读语义）----
    spent_tokens_in: int = 0
    spent_tokens_out: int = 0
    spent_cost: float = 0.0
