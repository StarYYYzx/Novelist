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


class OpenAICompatibleProvider:
    """OpenAI 兼容 chat completions 适配器。"""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str = "gpt-4o-mini",
        timeout_s: float = 60.0,
    ) -> None:
        if httpx is None:
            raise ProviderError("httpx not installed (pip install novelist[providers])")
        self.base_url = (base_url or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.model = model
        self.timeout_s = timeout_s

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
        payload: dict = {
            "model": req.model or self.model,
            "messages": [{"role": m.role, "content": m.content} for m in req.messages],
        }
        if req.tools:
            payload["tools"] = req.tools
        if req.temperature is not None:
            payload["temperature"] = req.temperature
        if req.response_format == "json_object":
            payload["response_format"] = {"type": "json_object"}
        if req.max_tokens_out:
            payload["max_tokens"] = req.max_tokens_out

        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            resp = httpx.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=headers,
                timeout=self.timeout_s,
            )
        except httpx.HTTPError as e:  # type: ignore
            raise ProviderError(f"openai request failed: {e}") from e

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
            body = resp.text[:200]
            # OpenAI 错误体常含 "type"/"code"
            raise ProviderError(f"openai http {resp.status_code}: {body}")

        return parse_completion(resp.status_code, resp.json())


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
    )
