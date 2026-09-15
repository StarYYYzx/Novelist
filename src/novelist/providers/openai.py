"""OpenAI 兼容协议适配器（docs/07 §2.4，first real provider）。

- 协议：POST {base}/chat/completions（兼容 OpenAI / DeepSeek / vLLM / Ollama-openai-gate）。
- 能力降级：按 ProviderCapabilities 声明；无 key/不可达时抛出 ProviderError（上层按 ADR-006 降级）。
- 审核拦截识别（ADR-015）：HTTP 451 / 消息体含 moderation safe 字段 / finish_reason 异常 -> 标记 blocked。

依赖 httpx（pyproject providers extras）。
"""

from __future__ import annotations

import dataclasses
import json
import os
import time

try:
    import httpx  # type: ignore
except ImportError:  # pragma: no cover - 可选依赖
    httpx = None  # type: ignore

from ..core.errors import ProviderError
from ..core.llm import (
    LLMRequest,
    LLMResult,
    ProviderCapabilities,
    ToolCall,
    Usage,
)
from .secrets import redact_message

# AG-16（2026-09-15 审计）：瞬时失败（限流/网关抖动/连接错误）此前**无重试**——
# 30 轮的工具循环里第 3 轮撞一次 429 就会把整章打挂。这里做有限退避重试。
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


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
        max_retries: int = 2,
        retry_backoff_s: float = 0.8,
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
        # 瞬时错误重试（AG-16）：默认 2 次、指数退避；0 可关。
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_s = max(0.0, float(retry_backoff_s))

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

        # 原始调用日志（core/calllog）：完整 prompt + 实际请求体 + 原始响应 + 解析结果
        # + 异常 + 耗时。记录失败绝不影响生成/回写（与 _write_generation_audit 同哲学）。
        import time as _t
        from ..core import calllog as _cl

        t0 = _t.perf_counter()
        status_code: int | None = None
        raw_text: str | None = None
        result: LLMResult | None = None
        payload: dict | None = None
        # D7（2026-09-15）：不用 `sys.exc_info()` 判断"本次调用是否抛异常"。它的语义是
        # "当前正在处理的异常"——一旦出现"在 except 块里重试 complete()"的写法
        # （F14.1 的改写重试 / 运行时切换 Provider 正是这种形态），**成功的调用**会被
        # 记上外层那条假异常。显式变量无此歧义。
        exc: BaseException | None = None
        try:
            payload = build_payload(
                req, self.model, supports_reasoning_roundtrip=self.supports_reasoning_roundtrip
            )

            headers = {"Authorization": f"Bearer {self.api_key}"}
            # 瞬时错误退避重试（AG-16）：429/5xx/连接错误重试 max_retries 次；
            # 其余（4xx 参数错、审核拦截）立即上抛，不做无意义重试。
            attempt = 0
            while True:
                try:
                    resp = httpx.post(
                        f"{self.base_url}/chat/completions",
                        json=payload,
                        headers=headers,
                        timeout=self.timeout_s,
                    )
                except httpx.HTTPError as e:  # type: ignore
                    if attempt < self.max_retries:
                        attempt += 1
                        time.sleep(self.retry_backoff_s * attempt)
                        continue
                    raise ProviderError(
                        f"openai request failed: {redact_message(str(e), [self.api_key])}"
                    ) from e
                if resp.status_code in _RETRY_STATUS and attempt < self.max_retries:
                    attempt += 1
                    time.sleep(self.retry_backoff_s * attempt)
                    continue
                break

            status_code = resp.status_code
            raw_text = resp.text
            if resp.status_code == 451:  # 法律/审核拦截
                result = LLMResult(
                    ok=False,
                    blocked=True,
                    block_reason="http_451",
                    provider_note="",
                    finish_reason="error",
                    provider="openai",
                )
                return result
            if resp.status_code != 200:
                body = redact_message(resp.text[:200], [self.api_key])
                # OpenAI 错误体常含 "type"/"code"
                raise ProviderError(f"openai http {resp.status_code}: {body}")

            result = parse_completion(resp.status_code, resp.json())
            return result
        except BaseException as e:  # noqa: BLE001 - 只做留痕，异常原样上抛
            exc = e
            raise
        finally:
            try:
                _cl.ensure_enabled()   # 自动开启（`NOVELIST_CALLLOG` 可关；见 calllog 模块文档）
                _cl.record(_snapshot(
                    self, req, payload, status_code, raw_text, result,
                    exc=exc, elapsed_ms=(_t.perf_counter() - t0) * 1000.0,
                ))
            except Exception:  # noqa: BLE001 - 记录失败绝不阻断生成
                pass


def _redact_obj(o, secrets: list) -> object:
    """递归脱敏 dict/list 内的字符串（payload 结构浅且规整，成本可接受）。

    D4#3（2026-09-15）：ADR-035 §D 声明"payload / 原始文本 / 异常串经脱敏"，
    实测只有 raw_text 与 exception 走了脱敏，**payload 原样落盘**——payload 里
    正是完整 prompt 与设定正文，是体积与敏感度最大的一块。
    """
    if isinstance(o, str):
        return redact_message(o, secrets)
    if isinstance(o, dict):
        return {k: _redact_obj(v, secrets) for k, v in o.items()}
    if isinstance(o, list):
        return [_redact_obj(v, secrets) for v in o]
    return o


def _snapshot(provider, req: LLMRequest, payload: dict | None, status_code: int | None,
              raw_text: str | None, result: LLMResult | None, *,
              exc: BaseException | None, elapsed_ms: float) -> dict:
    """把一次完整 LLM 调用折叠成一条可 JSON 化的溯源记录（core/calllog.record 使用）。"""
    secrets = [getattr(provider, "api_key", None)]
    return {
        "provider": getattr(provider, "provider_name", None) or type(provider).__name__,
        "model": req.model or provider.model,
        "base_url": provider.base_url,
        "request": {
            # D4#4（2026-09-15）：完整 messages 已逐字存在 `payload.messages`（实际请求体），
            # 此处原先再存一份 → 磁盘体积白翻一倍（实测单条 ≥12KB、约 0.15–0.3GB/本）。
            # 改为只留**结构摘要**（角色 + 字数），完整内容以 payload 为准——
            # ADR-035 §C"宁全勿缺"指的是"不丢信息"，不是"同样内容存两遍"。
            "messages_digest": [
                {"role": m.role, "chars": len(m.content or "")} for m in req.messages
            ],
            "temperature": req.temperature,
            "response_format": req.response_format,
            "max_tokens_out": req.max_tokens_out,
            "max_content_tokens": req.max_content_tokens,
            "thinking": req.thinking,
            "reasoning_effort": req.reasoning_effort,
        },
        "payload": _redact_obj(payload, secrets) if payload is not None else None,
        "status_code": status_code,
        "response": {
            "raw_text": redact_message(raw_text, secrets) if raw_text is not None else None,
            "result": dataclasses.asdict(result) if result is not None else None,
        },
        "exception": {
            "type": exc.__class__.__name__,
            "message": redact_message(str(exc), secrets),
        }
        if exc is not None else None,
        "elapsed_ms": round(elapsed_ms, 3),
    }


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
    - 工具消息协议（AG-3，2026-09-15 审计）：assistant 消息必须**原样回传** `tool_calls`
      （含 id），工具结果必须以 `role="tool"` + `tool_call_id` 回灌——此前 LLMMessage
      只有 role/content，工具结果被拼成 role="user" 的自然语言，模型收到的多轮上下文
      是非法的，一接线就 400。
    - 正文预算：max_content_tokens 覆盖总预算（与思考型模型兼容，见 docs/人工审查第二批）。
    """
    messages: list[dict] = []
    for m in req.messages:
        d: dict = {"role": m.role, "content": m.content}
        if m.role == "assistant" and getattr(m, "tool_calls", None):
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments or {}, ensure_ascii=False),
                    },
                }
                for tc in m.tool_calls
            ]
        if m.role == "tool":
            d["tool_call_id"] = getattr(m, "tool_call_id", None) or ""
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
    usage_data = data.get("usage") or {}
    details = usage_data.get("completion_tokens_details") or {}
    reasoning_text = message.get("reasoning_content") or message.get("reasoning") or ""

    return LLMResult(
        ok=True,
        content=message.get("content") or "",
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        provider="openai",
        usage=Usage(
            tokens_in=usage_data.get("prompt_tokens", 0),
            tokens_out=usage_data.get("completion_tokens", 0),
            # DeepSeek 上下文缓存命中拆分（P0-1，2026-09-12 审计）；其余厂商不返回 → None
            cache_hit_tokens=usage_data.get("prompt_cache_hit_tokens"),
            cache_miss_tokens=usage_data.get("prompt_cache_miss_tokens"),
        ),
        reasoning=reasoning_text,
        reasoning_tokens=details.get("reasoning_tokens"),
    )
