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


@dataclass
class SubAgentRun:
    """证据循环（ADR-032 基座）的返回：最终结果 + 轮数 + 证据轨迹。

    `evidence` 是可审计的"取证过程"：每轮 LLM 决策 + 每次工具读取的观察，
    供 chronicler/reviewer 的敏感/冲突项在人工审时复查它"到底看了什么"。
    """

    final: str
    rounds: int
    evidence: list[dict]


class AgentRunner:
    """LLM 驱动的 Agent 循环（docs/04 §5.1）。"""

    def __init__(
        self,
        provider,
        session: SessionInfo,
        budget: Budget | None = None,
        registry: ToolRegistry | None = None,
        max_rounds: int = 30,
        *,
        thinking: bool | None = None,
    ) -> None:
        self.provider = provider
        self.session = session
        self.budget = budget or Budget(max_tokens_out=4000, max_rounds=max_rounds)
        self.registry = registry
        self.thinking = thinking  # None=跟随 Provider；True/False 显式覆盖（ADR-032 基座）
        self._rf: str = "text"
        self._evidence: list[dict] = []
        self._messages: list[LLMMessage] = []

    # ---- 决策来源：真实 LLM，或测试注入的决策者 ----
    def _decide(self, context: str) -> AgentDecision:
        """默认经 LLM 决策（见 run_evidence）；子类/测试可替换。"""
        self._messages.append(LLMMessage(role="user", content=context))
        result = self.provider.complete(
            LLMRequest(messages=self._messages, max_tokens_out=self.budget.max_tokens_out,
                       response_format=self._rf,
                       thinking=self.thinking)  # 工具多轮编辑 Agent 循环显式关思考；判断型子代理（ADR-032）可开
        )
        self._messages.append(LLMMessage(role="assistant", content=self._fmt_result(result)))
        return _parse_decision(result)

    @property
    def evidence(self) -> list[dict]:
        """本轮运行累计的证据轨迹（ADR-032）。"""
        return list(self._evidence)

    def system(self, prompt: str) -> None:
        self._messages.append(LLMMessage(role="system", content=prompt))

    def run_loop(self, goal: str, max_rounds: int | None = None) -> str:
        """执行 Agent 循环直到 LLM 给出 final 或达到上限。返回最终结果文本。"""
        return self.run_evidence(goal, max_rounds=max_rounds).final

    def run_evidence(
        self,
        goal: str,
        *,
        system_prompt: str | None = None,
        max_rounds: int | None = None,
        response_format: str = "text",
    ) -> SubAgentRun:
        """证据循环（ADR-032 统一子代理基座）。

        与 `run_loop` 同一决策环，但：可选注入角色 persona；可按角色显式设
        `system_prompt`/`response_format`（判断型任务可 `json_object`）；并把每轮
        决策与工具观察记入 `_evidence`，最后以 `SubAgentRun` 返回带审计轨迹的结果。

        语义不变：LLM 可先经 registry 读证据（query_memory/read_file…）再做决策，
        直到无工具调用（final）或达轮次上限；上限抛 `AgentLoopError` 强制收敛。
        """
        if system_prompt is not None:
            self.system(system_prompt)
        self._rf = response_format
        rounds = max_rounds or (self.budget.max_rounds or 30)
        self._messages.append(LLMMessage(role="user", content=f"目标：{goal}"))
        for i in range(rounds):
            result = self._decide(f"当前目标：{goal}\n请基于已有证据决定下一步（读证据或输出结论）。")
            detail = (
                result.final
                if result.final is not None
                else f"tool={result.tool_name} args={result.tool_args}"
            )
            self._evidence.append({"kind": "decide", "round": i + 1, "detail": detail})
            if result.final is not None:
                self._evidence.append({"kind": "final", "round": i + 1, "content": result.final})
                return SubAgentRun(final=result.final, rounds=i + 1, evidence=self._evidence)
            if result.tool_name is None:
                raise AgentLoopError("LLM 未给出工具调用也未结束")
            self._run_tool(result)
        # 达到轮次上限——强制收敛
        raise AgentLoopError("agent loop reached round limit")

    def _run_tool(self, d: AgentDecision) -> None:
        if self.registry is None:
            raise AgentLoopError("no tool registry bound")
        res = self.registry.invoke(self.session, d.tool_name or "", d.tool_args or {})
        self._evidence.append({"kind": "tool", "tool": d.tool_name, "ok": res.code, "data": res.data})
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
