"""Agent 循环执行器（docs/04 §5.1 / docs/05 §2.3，M1 落地）。

主编剧与子代理共享同一套循环原语：系统提示 + 权限面 + 预算不同。
循环语义（docs/04 §5.1）：
  observe → think → act(tool|spawn|return) → observe ...
支持：
- LLM 驱动的结构化决策（每条结果含系统消息 + 若干动作）。
- 工具经 ToolRegistry 执行（受门禁），结果 append 回消息流供下轮观察。
- 步数/预算上限收敛（docs/04 §5.1：强制收敛）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .llm import LLMMessage, LLMRequest, LLMResult, ToolCall
from .session import Budget, SessionInfo
from .tools import ToolRegistry


class AgentLoopError(Exception):
    pass


@dataclass
class AgentDecision:
    """一轮循环中 LLM 产生的动作（1 个工具调用 或 结束）。"""

    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    final: str | None = None  # 结束时返回给编排器的结果
    note: str | None = None


class AgentRunner:
    """LLM 驱动的 Agent 循环（docs/04 §5.1）。"""

    def __init__(
        self,
        provider,
        session: SessionInfo,
        budget: Budget | None = None,
        registry: ToolRegistry | None = None,
        max_rounds: int = 30,
    ) -> None:
        self.provider = provider
        self.session = session
        self.budget = budget or Budget(max_tokens_out=4000, max_rounds=max_rounds)
        self.registry = registry
        self._messages: list[LLMMessage] = []

    # ---- 决策来源：真实 LLM，或测试注入的决策者 ----
    def _decide(self, context: str) -> AgentDecision:
        """默认经 LLM 决策（见 run_loop）；子类/测试可替换。"""
        self._messages.append(LLMMessage(role="user", content=context))
        result = self.provider.complete(
            LLMRequest(messages=self._messages, max_tokens_out=self.budget.max_tokens_out,
                       thinking=False)  # 工具多轮 Agent 循环：显式关思考（DeepSeek 工具多轮开思考须回传 reasoning_content）
        )
        self._messages.append(LLMMessage(role="assistant", content=self._fmt_result(result)))
        return _parse_decision(result)

    def system(self, prompt: str) -> None:
        self._messages.append(LLMMessage(role="system", content=prompt))

    def run_loop(self, goal: str, max_rounds: int | None = None) -> str:
        """执行 Agent 循环直到 LLM 给出 final 或达到上限。返回最终结果文本。"""
        rounds = max_rounds or (self.budget.max_rounds or 30)
        self._messages.append(LLMMessage(role="user", content=f"目标：{goal}"))
        for _ in range(rounds):
            result = self._decide(f"当前目标：{goal}\n请决定下一步动作。")
            if result.final is not None:
                return result.final
            if result.tool_name is None:
                raise AgentLoopError("LLM 未给出工具调用也未结束")
            self._run_tool(result)
        # 达到轮次上限——强制收敛
        raise AgentLoopError("agent loop reached round limit")

    def _run_tool(self, d: AgentDecision) -> None:
        if self.registry is None:
            raise AgentLoopError("no tool registry bound")
        res = self.registry.invoke(self.session, d.tool_name or "", d.tool_args or {})
        # 把工具结果作为观察 append 回去
        self._messages.append(
            LLMMessage(role="user", content=f"tool {d.tool_name} -> {res.code}: {res.data}")
        )

    def _fmt_result(self, r: LLMResult) -> str:
        if r.tool_calls:
            return " ".join(f"[{tc.name}({tc.arguments})]" for tc in r.tool_calls)
        return r.content or ""


def _parse_decision(r: LLMResult) -> AgentDecision:
    """从 LLM 结果解析一轮决策：优先用 tool_calls，否则视 content 为最终结果。"""
    if r.tool_calls:
        tc: ToolCall = r.tool_calls[0]
        return AgentDecision(tool_name=tc.name, tool_args=tc.arguments or {})
    # 无 tool_calls → 把 content 当最终输出
    return AgentDecision(final=r.content or "")
