"""Agent 循环执行器（docs/04 §5.1 / docs/05 §2.3，M1 落地）。

主编剧与子代理共享同一套循环原语：系统提示 + 权限面 + 预算不同。
循环语义（docs/04 §5.1）：
  observe → think → act(tool|spawn|return) → observe ...
支持：
- LLM 驱动的结构化决策（每条结果含系统消息 + 若干动作）。
- 工具经 ToolRegistry 执行（受门禁），结果 append 回消息流供下轮观察。
- 步数/预算上限收敛（docs/04 §5.1：强制收敛）。

**2026-09-15 审计修复（AG-2/3/11/12/13/17/20/21）**：
- 原生 function calling **真正接线**：向模型下发 `tools`（OpenAI function schema，不含内部
  `level`），并按 OpenAI 消息协议回传 assistant 的 `tool_calls` + `role="tool"` 结果
  （此前 `_decide()` 从不传 tools、`list_defs()` 全库零调用 → 真机永远拿不到工具调用，
  Agent 循环退化为"单轮直出"，ADR-032 的证据环同样是空转）。
- 能力门控：provider 未声明 `capabilities.tool_calling` 时**不下发 tools**（不支持的后端
  收到 tools 会报错或胡乱回），由调用方决定降级。
- 一轮内**全部** tool_calls 都执行（此前只取第 0 个，其余静默丢弃）。
- 观测有界：单次截断 + 整轮总预算（docs/04 §5.3 的上下文预算在循环侧落地）。
- 成本记账：逐轮累计 tokens/成本写入 `Budget` 与证据轨迹。
- 空响应 / 审核拦截**不再被当成最终结果**（此前 `content=""` + 无 tool_calls 会被
  当作 final 返回，调用方以为成功，产出空章）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .llm import (
    LLMMessage,
    LLMRequest,
    LLMResult,
    ModerationBlockedError,
    ToolCall,
    estimate_cost,
)
from .session import Budget, SessionInfo
from .tools import ToolRegistry

# 观测上限（拍板：单次 8k 字符；整轮总量 32k——超过即不再回传工具输出）
OBS_LIMIT_CHARS = 8000
OBS_TOTAL_BUDGET_CHARS = 32_000


class AgentLoopError(Exception):
    pass


@dataclass
class AgentDecision:
    """一轮循环中 LLM 产生的动作（1 个或多个工具调用 或 结束）。

    `tool_name`/`tool_args` 保留"第一个调用"的镜像（向后兼容既有调用方与测试）；
    完整列表看 `tool_calls`。
    """

    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    final: str | None = None  # 结束时返回给编排器的结果
    note: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class SubAgentRun:
    """证据循环（ADR-032 基座）的返回：最终结果 + 轮数 + 证据轨迹。

    `evidence` 是可审计的"取证过程"：每轮 LLM 决策 + 每次工具读取的观察，
    供 chronicler/reviewer 的敏感/冲突项在人工审时复查它"到底看了什么"。
    """

    final: str
    rounds: int
    evidence: list[dict]
    usage: dict[str, Any] = field(default_factory=dict)  # 累计用量（AG-12）


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
        tool_profile: str | None = None,
        obs_limit_chars: int = OBS_LIMIT_CHARS,
        obs_total_budget_chars: int = OBS_TOTAL_BUDGET_CHARS,
        enforce_cost: bool = False,
    ) -> None:
        self.provider = provider
        self.session = session
        self.budget = budget or Budget(max_tokens_out=4000, max_rounds=max_rounds)
        self.registry = registry
        self.thinking = thinking  # None=跟随 Provider；True/False 显式覆盖（ADR-032 基座）
        self.obs_limit_chars = obs_limit_chars
        self.obs_total_budget_chars = obs_total_budget_chars
        self.enforce_cost = enforce_cost  # 拍板：默认只记账，不硬停
        self._rf: str = "text"
        self._evidence: list[dict] = []
        self._messages: list[LLMMessage] = []
        self._obs_chars = 0
        # 对话态工具调用播报（2026-09-19，真机 UX）：console 置 True 时每次调用
        # 经 emit 打一行「→ 调用 xxx(...)」——此前整轮静默，用户看不见 agent 在做什么。
        self.trace_tools = False
        # 原生工具：有 registry + provider 声明支持 tool_calling 才下发（AG-20 能力门控）
        caps = getattr(provider, "capabilities", None)
        self.native_tools = bool(
            registry is not None and getattr(caps, "tool_calling", False)
        )
        self._tool_schemas = (
            registry.to_openai_schema(profile=tool_profile) if self.native_tools else None
        )
        self._used_in = 0
        self._used_out = 0
        self._cost = 0.0

    # ---- 决策来源：真实 LLM，或测试注入的决策者 ----
    def _decide(self, context: str | None) -> AgentDecision:
        """默认经 LLM 决策（见 run_evidence）；子类/测试可替换。

        `context=None`（chat 模式首轮）时不追加任何提示——用户消息本身已是 prompt。
        """
        if context:
            self._messages.append(LLMMessage(role="user", content=context))
        result = self.provider.complete(
            # 传**快照**（list 拷贝）：请求一旦发出就不该随后续 append 变化，
            # 否则任何持有 req 的一方（calllog/测试/审计）看到的都是"未来的消息流"。
            LLMRequest(messages=list(self._messages), max_tokens_out=self.budget.max_tokens_out,
                       response_format=self._rf,
                       tools=self._tool_schemas,  # AG-2：真正把工具定义发出去
                       thinking=self.thinking)  # 工具多轮编辑 Agent 循环显式关思考；判断型子代理（ADR-032）可开
        )
        self._account(result)
        if getattr(result, "blocked", False):
            # 审核拦截（docs/04 §5.10）：绝不能当成"最终结果"静默吞掉（AG-5）
            raise ModerationBlockedError(result.block_reason, result.provider_note)
        self._messages.append(LLMMessage(
            role="assistant",
            content=result.content or "",
            reasoning_content=getattr(result, "reasoning", "") or "",
            tool_calls=list(result.tool_calls) or None,  # AG-3：回传时必须带 tool_calls
        ))
        return _parse_decision(result)

    def _account(self, result: LLMResult) -> None:
        """逐轮累计用量（AG-12）；超 `max_cost` 且显式启用硬停时抛错。"""
        usage = getattr(result, "usage", None)
        if usage is None:
            return
        self._used_in += int(getattr(usage, "tokens_in", 0) or 0)
        self._used_out += int(getattr(usage, "tokens_out", 0) or 0)
        cost = getattr(usage, "cost_estimate", None)
        if cost is None:
            cost = estimate_cost(
                cache_hit=int(getattr(usage, "cache_hit_tokens", 0) or 0),
                cache_miss=int(getattr(usage, "cache_miss_tokens", 0) or 0)
                or int(getattr(usage, "tokens_in", 0) or 0),
                tokens_out=int(getattr(usage, "tokens_out", 0) or 0),
            )
        self._cost += float(cost or 0.0)
        self.budget.spent_tokens_in = self._used_in
        self.budget.spent_tokens_out = self._used_out
        self.budget.spent_cost = round(self._cost, 6)
        if self.enforce_cost and self.budget.max_cost and self._cost > self.budget.max_cost:
            raise AgentLoopError(
                f"cost budget exceeded: ¥{self._cost:.4f} > ¥{self.budget.max_cost:.4f}"
            )

    @property
    def usage(self) -> dict[str, Any]:
        """累计用量（写审计/证据轨迹用）。"""
        return {
            "tokens_in": self._used_in,
            "tokens_out": self._used_out,
            "cost": round(self._cost, 6),
            "rounds_budget": self.budget.max_rounds,
        }

    @property
    def evidence(self) -> list[dict]:
        """本轮运行累计的证据轨迹（ADR-032）。"""
        return list(self._evidence)

    def system(self, prompt: str) -> None:
        self._messages.append(LLMMessage(role="system", content=prompt))

    # ---- M3ac（ADR-036 常驻对话 Agent）：会话化 API ----

    def load_messages(self, messages: list[dict]) -> None:
        """回放会话历史（M3ac-3 持久化的读侧）。

        只接受 `user`/`assistant`/`system` 三种 role——tool 消息脱离配对的
        assistant.tool_calls 在 OpenAI 协议里非法，跨会话回放只保留文本轮
        （工具的探索过程是当轮的临时物，不属于长期记忆）。
        """
        self._messages = [
            LLMMessage(role=str(m["role"]), content=str(m.get("content") or ""))
            for m in messages
            if isinstance(m, dict) and m.get("role") in ("system", "user", "assistant")
        ]

    def export_messages(self) -> list[dict]:
        """导出当前消息流（浅拷贝 dict，调用方改不坏内部状态）。"""
        return [{"role": m.role, "content": m.content} for m in self._messages]

    def run_chat(self, user_text: str, *, max_rounds: int | None = None) -> SubAgentRun:
        """对话态一轮（ADR-036）：用户一句话 → 自主调工具 → 给出回答。

        与 `run_evidence` 同一决策环，差异（S-2）：
        - 用户消息**原样**进入消息流，不包「目标：X」前缀；
        - 首轮零提示词（用户消息本身就是 prompt）；后续轮用中性继续语，
          不再是取证子代理口吻的「读证据或输出结论」。
        结束判据：模型给出无工具调用的文本（final）或达轮次上限（抛 AgentLoopError，
        调用方可 converge() 抢救——与批式同语义）。
        """
        self._messages.append(LLMMessage(role="user", content=user_text))
        self._rf = "text"
        # 观测预算是**单轮**语义（真机 bug 修复 2026-09-19）：chat 复用同一 runner 跨轮，
        # 不重置会让上一轮的读取把 32k 预算烧光，下一轮"工具已不可再调用"。
        self._obs_chars = 0
        rounds = max_rounds or (self.budget.max_rounds or 30)
        for i in range(rounds):
            ctx = (None if i == 0 else
                   "（基于上面的工具结果继续回答用户；已读过的内容不要重复罗列）")
            try:
                result = self._decide(ctx)
            except AgentLoopError as e:
                self._evidence.append({"kind": "error", "round": i + 1, "detail": str(e)})
                raise
            calls = result.tool_calls or []
            if result.final is not None:
                self._evidence.append({"kind": "final", "round": i + 1,
                                       "content": result.final})
                return SubAgentRun(final=result.final, rounds=i + 1,
                                   evidence=self._evidence, usage=self.usage)
            self._evidence.append({
                "kind": "decide", "round": i + 1,
                "detail": "; ".join(f"tool={tc.name}" for tc in calls)})
            if not calls:
                raise AgentLoopError("LLM 未给出工具调用也未结束")
            for tc in calls:
                self._trace(tc)
                self._run_tool(AgentDecision(tool_name=tc.name, tool_args=tc.arguments,
                                             tool_calls=[tc]))
        self._evidence.append({"kind": "round_limit", "round": rounds, "detail": "达到轮次上限"})
        raise AgentLoopError("agent loop reached round limit")

    def _trace(self, tc: ToolCall) -> None:
        """工具调用播报（trace_tools=True 时）：对齐编码 agent 的「正在读 xxx」可见性。"""
        if not self.trace_tools:
            return
        from .output import emit

        args = json.dumps(tc.arguments or {}, ensure_ascii=False, default=str)
        if len(args) > 80:
            args = args[:77] + "..."
        emit(f"  → 调用 {tc.name}({args})")

    def run_loop(self, goal: str, max_rounds: int | None = None) -> str:
        """执行 Agent 循环直到 LLM 给出 final 或达到上限。返回最终结果文本。"""
        return self.run_evidence(goal, max_rounds=max_rounds).final

    def converge(self, note: str = "") -> str:
        """轮次耗尽后的**强制收敛**调用（docs/04 §5.1，AG-13 / 拍板 A）。

        复用已积累的消息流，明确要求"不得再调用工具，直接给出当前已完成内容与未完成项"。
        由调用方（orchestrator）在"未落盘且需要抢救内容"时使用；本方法不做落盘判断。
        """
        self._messages.append(LLMMessage(
            role="user",
            content=("已达到本轮的工具调用/步数上限。**不要再调用任何工具**。"
                     "请直接输出：① 当前已完成的正文（若有，全文原样输出）"
                     "② 未完成项清单。不要解释过程。" + (f"\n补充要求：{note}" if note else "")),
        ))
        result = self.provider.complete(
            LLMRequest(messages=self._messages, max_tokens_out=self.budget.max_tokens_out,
                       response_format="text", tools=None, thinking=self.thinking)
        )
        self._account(result)
        self._evidence.append({"kind": "converge", "detail": (result.content or "")[:200]})
        return result.content or ""

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
        直到无工具调用（final）或达轮次上限；上限抛 `AgentLoopError`（由调用方决定
        是走 `converge()` 强制收敛，还是按失败收尾——AG-13 拍板 A 的划分点）。
        """
        if system_prompt is not None:
            self.system(system_prompt)
        self._rf = response_format
        rounds = max_rounds or (self.budget.max_rounds or 30)
        # AG-21：目标只注入一次（此前"目标：X" + 每轮"当前目标：X…" 会重复膨胀上下文）
        self._messages.append(LLMMessage(role="user", content=f"目标：{goal}"))
        for i in range(rounds):
            ctx = ("请基于已有证据决定下一步（读证据或输出结论）。"
                   if i == 0 else
                   f"第 {i + 1}/{rounds} 轮：基于上一步的观察继续决定（读证据或输出结论）。")
            try:
                result = self._decide(ctx)
            except AgentLoopError as e:  # 预算/空响应类错误：留痕后向上
                self._evidence.append({"kind": "error", "round": i + 1, "detail": str(e)})
                raise
            calls = result.tool_calls or []
            if result.final is not None:
                self._evidence.append({"kind": "decide", "round": i + 1, "detail": result.final})
                self._evidence.append({"kind": "final", "round": i + 1, "content": result.final})
                return SubAgentRun(final=result.final, rounds=i + 1, evidence=self._evidence,
                                   usage=self.usage)
            detail = "; ".join(f"tool={tc.name} args={tc.arguments}" for tc in calls) or (
                f"tool={result.tool_name} args={result.tool_args}")
            self._evidence.append({"kind": "decide", "round": i + 1, "detail": detail})
            if not calls:
                raise AgentLoopError("LLM 未给出工具调用也未结束")
            for tc in calls:  # AG-17：一轮内的多个 tool_calls 全部执行，不再丢弃
                self._trace(tc)
                self._run_tool(AgentDecision(tool_name=tc.name, tool_args=tc.arguments,
                                             tool_calls=[tc]))
        # 达到轮次上限——交给调用方决定收敛策略（AG-13）
        self._evidence.append({"kind": "round_limit", "round": rounds, "detail": "达到轮次上限"})
        raise AgentLoopError("agent loop reached round limit")

    def _run_tool(self, d: AgentDecision) -> None:
        if self.registry is None:
            raise AgentLoopError("no tool registry bound")
        res = self.registry.invoke(self.session, d.tool_name or "", d.tool_args or {},
                                   budget=self.budget)  # AG-12：预算透传给工具
        self._evidence.append({"kind": "tool", "tool": d.tool_name, "ok": res.code, "data": res.data})
        # 把工具结果作为观察 append 回去，并施加观测上限（AG-11）
        self._messages.append(LLMMessage(
            role="tool" if self.native_tools else "user",
            content=self._observe(d.tool_name or "", res.code, res.data),
            tool_call_id=(d.tool_calls[0].id if d.tool_calls else None),
        ))

    def _observe(self, tool_name: str, code: str, data: Any) -> str:
        """渲染工具观察，带单次与总量两级截断（AG-11）。"""
        try:
            body = json.dumps(data, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001 - 不可序列化时退回 repr
            body = repr(data)
        if self._obs_chars >= self.obs_total_budget_chars:
            return (f"tool {tool_name} -> {code}: [观测预算已用尽（"
                    f"{self.obs_total_budget_chars} 字符），不再回传工具输出]")
        limit = min(self.obs_limit_chars, self.obs_total_budget_chars - self._obs_chars)
        if len(body) > limit:
            body = body[:limit] + f"…[已截断，原 {len(body)} 字符]"
        self._obs_chars += len(body)
        return f"tool {tool_name} -> {code}: {body}"


def _parse_decision(r: LLMResult) -> AgentDecision:
    """从 LLM 结果解析一轮决策：优先用 tool_calls，否则视 content 为最终结果。

    AG-5：`content` 为空且无 tool_calls 时**不再**当作"最终结果"（那会让上层拿到
    空成稿却判成功）；抛 `AgentLoopError` 让调用方按失败/降级处理。
    """
    if r.tool_calls:
        first: ToolCall = r.tool_calls[0]
        return AgentDecision(tool_name=first.name, tool_args=first.arguments or {},
                             tool_calls=list(r.tool_calls))
    content = (r.content or "").strip()
    if not content:
        raise AgentLoopError(f"LLM 返回空内容（finish_reason={getattr(r, 'finish_reason', '?')}）")
    # 无 tool_calls → 把 content 当最终输出
    return AgentDecision(final=r.content or "")
