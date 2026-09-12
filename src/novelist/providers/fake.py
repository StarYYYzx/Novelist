"""确定性 Fake Provider（测试与集成用，不调真实 LLM）。

遵守"单元测试绝不真正调 LLM"金法则（docs/09 §2.1）。
"""

from __future__ import annotations

from ..core.llm import (
    LLMRequest,
    LLMResult,
    ProviderCapabilities,
    Usage,
)


class FakeProvider:
    """返回固定/可编程内容的 LLM Provider。"""

    def __init__(
        self,
        *,
        reply: str = "ok",
        blocked: bool = False,
        block_reason: str | None = None,
    ) -> None:
        self._reply = reply
        self._blocked = blocked
        self._block_reason = block_reason

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            tool_calling=False,
            max_context=8000,
            json_mode=True,
            streaming=False,
            embedding=False,
        )

    def complete(self, req: LLMRequest) -> LLMResult:
        if self._blocked:
            return LLMResult(
                ok=False,
                blocked=True,
                block_reason=self._block_reason,
                finish_reason="error",
                provider="fake",
            )
        return LLMResult(
            ok=True,
            content=self._reply,
            finish_reason="stop",
            provider="fake",
            usage=Usage(tokens_in=10, tokens_out=10),
        )


class ScriptedProvider:
    """按预设脚本依次返回 tool_calls / 最终文本的可编程 Provider。

    用于驱动 AgentRunner 循环的集成测试（docs/04 §5.1）：脚本每项为一轮决策。
    每个 item：
      - {"tool": "write_draft", "args": {...}}  -> 触发一次工具调用
      - {"final": "文本"}                        -> 结束语
    """

    def __init__(self, script: list[dict]) -> None:
        self.script = list(script)
        self.calls = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            tool_calling=True, max_context=8000, json_mode=True, streaming=False, embedding=False
        )

    def complete(self, req: LLMRequest) -> LLMResult:
        try:
            step = self.script[self.calls]
        except IndexError:
            raise RuntimeError("ScriptedProvider script exhausted")
        self.calls += 1
        if "tool" in step:
            from ..core.llm import ToolCall

            return LLMResult(
                ok=True,
                content="",
                tool_calls=[ToolCall(id=f"c{self.calls}", name=step["tool"], arguments=step.get("args", {}))],
                finish_reason="tool_calls",
                provider="scripted",
                usage=Usage(tokens_in=0, tokens_out=0),
            )
        return LLMResult(
            ok=True,
            content=step.get("final", ""),
            finish_reason="stop",
            provider="scripted",
            usage=Usage(tokens_in=0, tokens_out=0),
        )

