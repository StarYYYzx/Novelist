"""LM-Studio 本地推理适配器（docs/07 §2.4，本地 OpenAI 兼容）。

- 协议：POST {base}/v1/chat/completions（LM-Studio 提供 OpenAI 兼容端点）。
- 默认 base_url = http://127.0.0.1:1234，model = qwen/qwen3.5-9b。
- 鉴权：可选 key。LM-Studio 可在设置中开启 API Key 保护（开启后须提供 key，
  关闭时 key 可留空）。key 来源：显式参数 > 环境变量 `LM_STUDIO_API_KEY` > 空。
- 本地小模型（9B）tool_calling 可能不稳：适配器把 LLM 返回的畸形 tool_calls
  容错为内容文本（上层按 ADR-006 降级），避免整轮失败。
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

DEFAULT_BASE = "http://127.0.0.1:1234"
DEFAULT_MODEL = "qwen/qwen3.5-9b"


class LMStudioProvider:
    """LM-Studio 本地模型适配器（OpenAI 兼容）。"""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout_s: float = 120.0,
        _client: "httpx.Client | None" = None,
    ) -> None:
        if httpx is None:
            raise ProviderError("httpx not installed (pip install novelist[providers])")
        self.base_url = (base_url or os.getenv("LM_STUDIO_BASE_URL") or DEFAULT_BASE).rstrip("/")
        self.api_key = api_key if api_key is not None else os.getenv("LM_STUDIO_API_KEY", "")
        self.model = model or os.getenv("LM_STUDIO_MODEL") or DEFAULT_MODEL
        self.timeout_s = timeout_s
        self._client = _client  # 测试注入 mock client

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            tool_calling=True,   # 声明支持；本地小模型实际可能不稳，由上层降级
            max_context=32_000,
            json_mode=True,
            streaming=False,
            embedding=False,     # 本地无 embedding，记忆检索走关键词降级（07§7.3）
        )

    def complete(self, req: LLMRequest) -> LLMResult:
        payload: dict = {
            "model": req.model or self.model,
            "messages": [{"role": m.role, "content": m.content} for m in req.messages],
            "temperature": req.temperature if req.temperature is not None else 0.7,
        }
        if req.tools:
            payload["tools"] = req.tools
        if req.response_format == "json_object":
            payload["response_format"] = {"type": "json_object"}
        if req.max_tokens_out:
            payload["max_tokens"] = req.max_tokens_out

        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        client = self._client if self._client is not None else httpx
        try:
            resp = client.post(
                f"{self.base_url}/v1/chat/completions",
                json=payload,
                headers=headers,
                timeout=self.timeout_s,
            )
        except httpx.HTTPError as e:  # type: ignore
            raise ProviderError(f"lmstudio request failed: {e}") from e

        if resp.status_code == 401:
            raise ProviderError(
                "lmstudio 401: LM-Studio 启用了 API Key 保护。"
                "请关闭保护或在 LM_STUDIO_API_KEY 环境变量中提供 key"
            )
        if resp.status_code != 200:
            body = resp.text[:200]
            raise ProviderError(f"lmstudio http {resp.status_code}: {body}")

        return parse_lmstudio(resp.json())


def parse_lmstudio(data: dict) -> LLMResult:
    """解析 LM-Studio 响应为标准 LLMResult（纯函数，便于测试）。

    本地小模型 tool_calls 常畸形：message.tool_calls 缺失/空、arguments 非 JSON、
    或模型把工具意图写进 content。此处容错：能解析出 ToolCall 就返回，
    否则把 content 原样返回（上层按文本处理）。
    """
    choice = data["choices"][0]
    message = choice.get("message", {})
    finish_reason = choice.get("finish_reason", "stop")

    tool_calls = []
    for tc in message.get("tool_calls") or []:
        try:
            args = json.loads(tc["function"].get("arguments") or "{}")
        except ValueError:
            args = {}
        tool_calls.append(ToolCall(id=tc.get("id", ""), name=tc["function"]["name"], arguments=args))

    return LLMResult(
        ok=True,
        content=message.get("content") or "",
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        provider="lmstudio",
        usage=Usage(
            tokens_in=data.get("usage", {}).get("prompt_tokens", 0),
            tokens_out=data.get("usage", {}).get("completion_tokens", 0),
        ),
    )
