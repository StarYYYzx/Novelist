"""确定性 Fake Provider（测试与集成用，不调真实 LLM）。

遵守"单元测试绝不真正调 LLM"金法则（docs/09 §2.1）。
"""

from __future__ import annotations

from ..core.llm import (
    LLMMessage,
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
