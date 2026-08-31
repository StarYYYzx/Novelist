"""LLM Provider 适配器抽象（docs/07 §2）。

核心层只依赖本模块抽象，不 import 任何供应商 SDK。实现见 providers/。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class ProviderCapabilities:
    """供应商能力描述（docs/07 §2.1），供降级策略使用。"""

    def __init__(
        self,
        *,
        tool_calling: bool,
        max_context: int | None,
        json_mode: bool,
        streaming: bool,
        embedding: bool,
    ) -> None:
        self.tool_calling = tool_calling
        self.max_context = max_context
        self.json_mode = json_mode
        self.streaming = streaming
        self.embedding = embedding

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"ProviderCapabilities({vars(self)})"


@dataclass
class LLMMessage:
    role: str  # system | user | assistant
    content: str


@dataclass
class ToolCall:
    """结构化工具调用（供应商原生返回，格式随 provider 映射）。"""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMRequest:
    """LLM 请求契约（docs/07 §2.2）。"""

    messages: list[LLMMessage]
    model: str | None = None
    tools: list[dict[str, Any]] | None = None
    temperature: float | None = None
    response_format: str = "text"  # "json_object" | "text"
    max_tokens_out: int | None = None
    thinking: bool | None = None
    # 思考模式按请求控制（讨论：云端 llama.cpp 认 chat_template_kwargs.enable_thinking，
    # 判断类任务如审校开思考提 recall，生成类任务关思考保正文预算）：
    # None = 跟随 Provider 默认（enable_thinking 参数）；True/False 显式覆盖。


@dataclass
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0
    cost_estimate: float | None = None


@dataclass
class LLMResult:
    """LLM 响应契约（docs/07 §2.2 / §2.6，含审核拦截字段）。"""

    ok: bool
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage | None = None
    finish_reason: str = "stop"  # stop | tool_calls | length | error
    provider: str = ""
    degraded: bool = False
    blocked: bool = False
    block_reason: str | None = None
    provider_note: str | None = None
    # 思考型模型（如 qwen3.5）的推理过程。
    # 关键：思考 token **计入 max_tokens**（`usage.completion_tokens_details.reasoning_tokens`），
    # 会挤占正文预算——预算被思考吃光时 `content` 为空字符串，而 `finish_reason == "length"`。
    # 见 docs/人工审查.md 第二批第 1 条。
    reasoning: str = ""
    reasoning_tokens: int | None = None


class LLMProvider(Protocol):
    """统一 LLM 适配器接口（docs/07 §2.1）。"""

    def complete(self, req: LLMRequest) -> LLMResult: ...

    @property
    def capabilities(self) -> ProviderCapabilities: ...


class OAuthError(Exception):
    """认证/凭据错误。"""


class ModerationBlockedError(Exception):
    """供应商审核拦截（docs/07 §2.6 / §9，MODERATION_BLOCKED）。"""

    def __init__(self, reason: str | None = None, provider_note: str | None = None) -> None:
        super().__init__(reason or "moderation blocked")
        self.block_reason = reason
        self.provider_note = provider_note


class EmbeddingProvider(Protocol):
    """向量接口（docs/07 §2.1.1）。无 embedding 时 query_memory 自动降级为关键词检索。"""

    def embed(self, texts: list[str]) -> list[list[float]]: ...

    @property
    def dim(self) -> int: ...

    @property
    def kind(self) -> str: ...
