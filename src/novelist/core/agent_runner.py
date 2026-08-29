"""核心包：Agent 循环执行器（docs/04 §5.1 / docs/05 §2.3）。

主编剧与子代理共享同一套循环原语，仅系统提示 + 权限面 + 上下文供给不同。
本文件为脚手架桩：定义动作枚举与循环骨架，具体策略在 M1 实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .llm import LLMProvider

from .session import Budget, SessionInfo


class ActionKind(Enum):
    TOOL = "tool"
    SPAWN = "spawn"
    RETURN = "return"


@dataclass
class AgentAction:
    kind: ActionKind
    name: str | None = None
    params: dict | None = None
    summary: str | None = None


@dataclass
class AgentTurn:
    """一次 Agent 循环迭代的产出（供编排器消费）。"""

    actions: list[AgentAction] = field(default_factory=list)
    result: str | None = None
    final: bool = False


class AgentRunner:
    """Agent 循环执行器（脚手架桩）。具体策略 M1 落地。"""

    def __init__(self, provider: LLMProvider, session: SessionInfo, budget: Budget | None = None) -> None:
        self.provider = provider
        self.session = session
        self.budget = budget or Budget(max_tokens_out=4000)

    def run_loop(self, system_prompt: str, goal: str, max_rounds: int | None = None) -> AgentTurn:
        """执行一次 Agent 循环。脚手架仅返回占位结果；M1 实现真实 LLM 驱动。"""
        raise NotImplementedError  # pragma: no cover - M1 脚手架
