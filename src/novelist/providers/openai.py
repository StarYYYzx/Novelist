"""OpenAI 兼容协议适配器（docs/07 §2.4，first real provider）。

- 协议：POST {base}/chat/completions（兼容 OpenAI / DeepSeek / vLLM / Ollama-openai-gate）。
- 能力降级：按 ProviderCapabilities 声明；无 key/不可达时抛出 ProviderError（上层按 ADR-006 降级）。
- 审核拦截识别（ADR-015）：HTTP 451 / 消息体含 moderation safe 字段 / finish_reason 异常 -> 标记 blocked。

依赖 httpx（pyproject providers extras）。
"""

from __future__ import annotations

import json
import os

try:
    import httpx  # type: ignore
except ImportError:  # pragma: no cover - 可选依赖
    httpx = None  # type: ignore

from ..core.errors import ProviderError
from ..core.llm import (
    LLMMessage,
    LLMRequest,
    LLMResult,
    ProviderCapabilities,
    ToolCall,
    Usage,
)
from .secrets import redact_message


class OpenAICompatibleProvider:
    """OpenAI 兼容 chat completions 适配器。"""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str = "gpt-4o-mini",
        timeout_s: float = 60.0,
        supports_reasoning_roundtrip: bool = False,
    ) -> None:
        if httpx is None:
            raise ProviderError("httpx not installed (pip install novelist[providers])")
        self.base_url = (base_url or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.model = model
        self.timeout_s = timeout_s
        # 思考型后端的工具多轮：需回传 assistant 的 reasoning_content。
        # 仅对真正支持的后端（如 DeepSeek v4）开启，OpenAI 等不主动发，避免未知字段。
        self.supports_reasoning_roundtrip = supports_reasoning_roundtrip

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            tool_calling=True,  # OpenAI 原生支持 tool calling
            max_context=128_000,
            json_mode=True,
            streaming=False,
            embedding=True,  # openai 亦提供 embeddings（此处 chat 适配不实现，降级为 keyword 由上层处理）
        )

    def complete(self, req: LLMRequest) -> LLMResult:
        if not self.api_key:
            raise ProviderError("no api key; set OPENAI_API_KEY or pass api_key")
        payload = build_payload(
            req, self.model, supports_reasoning_roundtrip=self.supports_reasoning_roundtrip
        )

        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            resp = httpx.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=headers,
                timeout=self.timeout_s,
            )
        except httpx.HTTPError as e:  # type: ignore
            raise ProviderError(
                f"openai request failed: {redact_message(str(e), [self.api_key])}"
            ) from e

        if resp.status_code == 451:  # 法律/审核拦截
            return LLMResult(
                ok=False,
                blocked=True,
                block_reason="http_451",
                provider_note="",
                finish_reason="error",
                provider="openai",
            )
        if resp.status_code != 200:
            body = redact_message(resp.text[:200], [self.api_key])
            # OpenAI 错误体常含 "type"/"code"
            raise ProviderError(f"openai http {resp.status_code}: {body}")

        return parse_completion(resp.status_code, resp.json())


def build_payload(
    req: LLMRequest,
    model: str,
    *,
    supports_reasoning_roundtrip: bool = False,
) -> dict:
    """组装 OpenAI / DeepSeek 兼容 chat/completions 请求体（纯函数，便于测试）。

    - 思考（第二轮·DeepSeek v4）：`thinking` 顶层开关（enabled/disabled），
      `reasoning_effort` 控制强度；官方在**思考启用时忽略 temperature**，
      故 enabled 时不发 temperature。
    - 工具多轮：`supports_reasoning_roundtrip` 且 assistant 消息带 reasoning_content
      时回传，否则 400（DeepSeek 约束）。
    - 正文预算：max_content_tokens 覆盖总预算（与思考型模型兼容，见 docs/人工审查第二批）。
    """
    messages: list[dict] = []
    for m in req.messages:
        d: dict = {"role": m.role, "content": m.content}
        if supports_reasoning_roundtrip and m.role == "assistant" and m.reasoning_content:
            d["reasoning_content"] = m.reasoning_content
        messages.append(d)

    payload: dict = {"model": req.model or model, "messages": messages}
    if req.tools:
        payload["tools"] = req.tools

    thinking = req.thinking
    if thinking is True:
        payload["thinking"] = {"type": "enabled"}
    elif thinking is False:
        payload["thinking"] = {"type": "disabled"}
    if req.reasoning_effort and thinking is not False:
        payload["reasoning_effort"] = req.reasoning_effort
    # 思考 enabled 时 DeepSeek 忽略 temperature；disabled/None 照常下发。
    if req.temperature is not None and thinking is not True:
        payload["temperature"] = req.temperature

    if req.response_format == "json_object":
        payload["response_format"] = {"type": "json_object"}
    if req.max_tokens_out:
        payload["max_tokens"] = req.max_tokens_out
    if req.max_content_tokens and req.max_content_tokens > (req.max_tokens_out or 0):
        payload["max_tokens"] = req.max_content_tokens
    return payload


def parse_completion(status_code: int, data: dict) -> LLMResult:
    """从 OpenAI 兼容 chat/completions 响应解析为 LLMResult（纯函数，便于测试）。"""
    if status_code == 451:
        return LLMResult(ok=False, blocked=True, block_reason="http_451", provider="openai", finish_reason="error")

    choice = data["choices"][0]
    message = choice.get("message", {})
    finish_reason = choice.get("finish_reason", "stop")

    blocked = finish_reason == "content_filter" or bool(message.get("refusal"))
    if blocked:
        return LLMResult(
            ok=False,
            blocked=True,
            block_reason="content_filter" if finish_reason == "content_filter" else "refusal",
            provider_note=message.get("refusal"),
            finish_reason=finish_reason,
            provider="openai",
        )

    tool_calls = []
    for tc in message.get("tool_calls") or []:
        try:
            args = json.loads(tc["function"].get("arguments") or "{}")
        except ValueError:  # pragma: no cover - 供应商返回畸形参数
            args = {}
        tool_calls.append(ToolCall(id=tc["id"], name=tc["function"]["name"], arguments=args))

    # 思考型模型（DeepSeek v4 / qwen3.5 等）：推理文本在 message.reasoning_content，
    # 其 token 数在 usage.completion_tokens_details.reasoning_tokens（计入 max_tokens）。
    details = (data.get("usage") or {}).get("completion_tokens_details") or {}
    reasoning_text = message.get("reasoning_content") or message.get("reasoning") or ""

    return LLMResult(
        ok=True,
        content=message.get("content") or "",
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        provider="openai",
        usage=Usage(
            tokens_in=data.get("usage", {}).get("prompt_tokens", 0),
            tokens_out=data.get("usage", {}).get("completion_tokens", 0),
        ),
        reasoning=reasoning_text,
        reasoning_tokens=details.get("reasoning_tokens"),
    )
