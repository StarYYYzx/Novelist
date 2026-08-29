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
    """预算/配额（docs/07 §3.2、docs/06 §7）。"""

    max_tokens_out: int
    max_tokens_in: int | None = None
    max_cost: float | None = None
    max_rounds: int | None = None  # 循环/围读会轮次上限
