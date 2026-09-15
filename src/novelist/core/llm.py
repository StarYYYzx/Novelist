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
    role: str  # system | user | assistant | tool
    content: str
    # 思考型模型（DeepSeek v4 / qwen3.5 等）的推理文本，随 assistant 消息保存。
    # 工具多轮时必须回传（DeepSeek 官方：若最终一次回答前曾发生工具调用，
    # 后续请求须携带上一 assistant 的 reasoning_content，否则返回 400）。
    reasoning_content: str = ""
    # 原生工具调用的消息协议（AG-3，2026-09-15 审计）：OpenAI 兼容接口要求
    #   assistant 消息带 `tool_calls`（并回传其 id），随后每条结果以
    #   `role="tool"` + `tool_call_id` 回灌——否则第二轮直接 400。
    # 此前只有 role/content，工具结果被拼成 role="user" 的文本，**结构上无法支持 FC**。
    tool_calls: list[ToolCall] | None = None  # 仅 assistant 消息
    tool_call_id: str | None = None           # 仅 role="tool" 消息


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
    # 正文预算（第二批·人工审查）：max_tokens_out 是**总输出预算**（思考+正文），
    # max_content_tokens 是**期望正文量**——适配层保证正文预算不被思考吃掉
    # （budget = max(总预算, 正文预算)），也供 reasoning_aware 判据使用。
    max_content_tokens: int | None = None
    # 思考模式按请求控制（讨论：云端 llama.cpp 认 chat_template_kwargs.enable_thinking，
    # 判断类任务如审校开思考提 recall，生成类任务关思考保正文预算）：
    # None = 跟随 Provider 默认（enable_thinking 参数）；True/False 显式覆盖。
    thinking: bool | None = None
    # 思考强度（DeepSeek v4 官方支持 "high" / "max"；跟随 thinking 一并下发；
    # thinking 显式 disabled 时不发）。其余后端忽略该字段。
    reasoning_effort: str | None = None


@dataclass
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0
    cost_estimate: float | None = None
    # 缓存命中/未命中输入 token（DeepSeek `usage.prompt_cache_hit_tokens` /
    # `prompt_cache_miss_tokens`）。DeepSeek 上下文硬盘缓存**自动生效、按前缀匹配**：
    # 命中 0.02 vs 未命中 1 元/百万（2026-09-10 起，50 倍差）——命中率是唯一决定输入
    # 成本的指标（P0-1，2026-09-12 prompt 审计）。未上报时保持 None，以区别于「上报了 0」：
    # 不可测时不伪造成 0。
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None


# DeepSeek Flash 系列单价（人民币元 / 百万 token）。参考价，仅供估算。
# 空闲时段档；高峰时段（工作日 9:00–12:00、14:00–18:00）各项翻倍。
# 落地用（P0-1）：把价表从各报告内联处集中到这里，并按命中/未命中分别计费——
# 旧口径只按 tokens_in 单一价算，系统性低估未命中成本、也看不见命中收益。
COST_PER_M_CNY = {
    "in_hit": 0.02,
    "in_miss": 1.0,
    "out": 2.0,  # 各来源口径有出入（2 元 vs 4 元），以官方价目页为准
}


def estimate_cost(*, cache_hit: int = 0, cache_miss: int = 0, tokens_out: int = 0) -> float:
    """按命中/未命中分别计费的估算成本（元）。"""
    p = COST_PER_M_CNY
    return (cache_hit * p["in_hit"] + cache_miss * p["in_miss"] + tokens_out * p["out"]) / 1_000_000


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
