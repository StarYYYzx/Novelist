"""DeepSeek 适配器 + API Key 隐私工具测试（不走网络，纯函数 + 构造断言）。

DeepSeek v4 思考模式（官方，2026-09）：
- `thinking` 按请求开关（enabled/disabled），思考启用时忽略 temperature；
- `reasoning_effort` 控制强度；
- 工具多轮必须回传 assistant 的 reasoning_content，否则 400；
- 思考文本在响应 message.reasoning_content，token 数在 usage.completion_tokens_details.reasoning_tokens。
"""

from __future__ import annotations

from novelist.core.llm import LLMMessage, LLMRequest
from novelist.providers.deepseek import DEEPSEEK_DEFAULT_MODEL, DeepSeekProvider, parse_deepseek
from novelist.providers.openai import build_payload, parse_completion
from novelist.providers.secrets import mask_secret, redact_message, resolve_api_key


def _req(**kw) -> LLMRequest:
    req = LLMRequest(messages=[LLMMessage(role="user", content="hi")])
    for k, v in kw.items():
        setattr(req, k, v)
    return req


# ---------------------------------------------------------------------------
# build_payload：thinking / reasoning_effort / temperature / 回传
# ---------------------------------------------------------------------------

def test_payload_thinking_enabled_drops_temperature_adds_effort():
    p = build_payload(
        _req(thinking=True, temperature=0.4, reasoning_effort="high"), "deepseek-v4-pro"
    )
    assert p["thinking"] == {"type": "enabled"}
    assert p["reasoning_effort"] == "high"
    assert "temperature" not in p  # 思考启用时官方忽略 temperature


def test_payload_thinking_disabled_keeps_temperature_drops_effort():
    p = build_payload(
        _req(thinking=False, temperature=0.4, reasoning_effort="high"), "deepseek-v4-pro"
    )
    assert p["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in p
    assert p["temperature"] == 0.4


def test_payload_thinking_none_no_thinking_key_and_keeps_temperature():
    p = build_payload(
        _req(thinking=None, temperature=0.7, reasoning_effort="max"), "deepseek-v4-pro"
    )
    assert "thinking" not in p
    assert p["temperature"] == 0.7
    assert p["reasoning_effort"] == "max"


def test_payload_roundtrip_reasoning_content_only_when_enabled():
    msg = LLMMessage(role="assistant", content="继续", reasoning_content="推理过程")
    req = LLMRequest(messages=[msg])

    p_on = build_payload(req, "deepseek-v4-pro", supports_reasoning_roundtrip=True)
    assert p_on["messages"][0]["reasoning_content"] == "推理过程"

    p_off = build_payload(req, "gpt-4o-mini", supports_reasoning_roundtrip=False)
    assert "reasoning_content" not in p_off["messages"][0]


def test_payload_plain_message_has_no_reasoning_key():
    p = build_payload(_req(), "deepseek-v4-pro", supports_reasoning_roundtrip=True)
    assert p["messages"][0] == {"role": "user", "content": "hi"}


# ---------------------------------------------------------------------------
# 响应解析：reasoning_content / reasoning_tokens
# ---------------------------------------------------------------------------

def test_parse_completion_extracts_reasoning():
    data = {
        "choices": [{
            "message": {"content": "正文", "reasoning_content": "思考过程"},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 30,
            "completion_tokens_details": {"reasoning_tokens": 12},
        },
    }
    res = parse_completion(200, data)
    assert res.reasoning == "思考过程"
    assert res.reasoning_tokens == 12
    assert res.content == "正文"


def test_parse_completion_no_reasoning_returns_empty():
    data = {
        "choices": [{"message": {"content": "正文"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 8},
    }
    res = parse_completion(200, data)
    assert res.reasoning == ""
    assert res.reasoning_tokens is None


def test_parse_deepseek_sets_provider_label():
    data = {
        "choices": [{"message": {"content": "正文"}, "finish_reason": "stop"}],
        "usage": {},
    }
    assert parse_deepseek(200, data).provider == "deepseek"


# ---------------------------------------------------------------------------
# DeepSeekProvider 构造：默认模型 / key 来源
# ---------------------------------------------------------------------------

def test_provider_default_model_is_v4_pro(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    p = DeepSeekProvider()
    assert p.model == DEEPSEEK_DEFAULT_MODEL
    assert DEEPSEEK_DEFAULT_MODEL.startswith("deepseek-v4")


def test_provider_key_precedence(monkeypatch):
    monkeypatch.setenv("DeepSeek-API-KEY", "legacy-key")
    # 新标准 env 优先于旧名
    p1 = DeepSeekProvider()
    assert p1.api_key == "legacy-key"

    monkeypatch.setenv("DEEPSEEK_API_KEY", "standard-key")
    p2 = DeepSeekProvider()
    assert p2.api_key == "standard-key"

    # 显式参数最高优先
    p3 = DeepSeekProvider(api_key="explicit-key")
    assert p3.api_key == "explicit-key"


def test_provider_roundtrip_flag_on():
    import os as _os

    _os.environ["DEEPSEEK_API_KEY"] = "sk-test"
    try:
        p = DeepSeekProvider()
        assert p.supports_reasoning_roundtrip is True
    finally:
        del _os.environ["DEEPSEEK_API_KEY"]


# ---------------------------------------------------------------------------
# API Key 隐私工具
# ---------------------------------------------------------------------------

def test_mask_secret():
    assert mask_secret("") == "(empty)"
    assert mask_secret("short") == "***"
    assert mask_secret("sk-abcdefghij12345678").endswith("…5678")
    assert "abcdefghij12345678" not in mask_secret("sk-abcdefghij12345678")


def test_resolve_api_key_ordering(monkeypatch):
    assert resolve_api_key("given", ["A"]) == "given"
    monkeypatch.setenv("A", "from-a")
    assert resolve_api_key(None, ["A", "B"]) == "from-a"
    assert resolve_api_key(None, []) is None


def test_redact_message_hides_secret():
    red = redact_message("401 invalid key sk-abcdefghij12345678 usage", ["sk-abcdefghij12345678"])
    assert "sk-abcdefghij12345678" not in red
    assert "…5678" in red


def test_redact_message_ignores_short_and_missing():
    assert redact_message("plain text", ["tiny"]) == "plain text"
    assert redact_message("plain text", ["absent-secret-value"]) == "plain text"
    assert redact_message("abc", []) == "abc"


def test_readme_smoke_env_key_never_in_plain_attr(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-verysecretk12345678")
    p = DeepSeekProvider()
    # 遮蔽版本可用作日志，且两者不要相等
    assert mask_secret(p.api_key) != p.api_key
    assert len(p.api_key) > len(mask_secret(p.api_key))


# ---------------------------------------------------------------------------
# 网关支持（2026-09-19）：base 覆盖 / 密钥与端点配对 / 思考字段可关
# ---------------------------------------------------------------------------

GATEWAY = "https://myai.bupt.edu.cn/llm-gw/v1"


def test_deepseek_default_endpoint_and_thinking_unchanged(monkeypatch):
    """未设任何环境变量时：官方端点 + 思考可用（回归保护）。"""
    for name in ("DEEPSEEK_API_BASE", "DEEPSEEK_GATEWAY_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    p = DeepSeekProvider(api_key="sk-test")
    assert p.base_url == "https://api.deepseek.com"
    assert p.supports_thinking is True
    assert p.supports_reasoning_roundtrip is True


def test_deepseek_gateway_base_disables_thinking(monkeypatch):
    """走网关（base 非官方）→ 自动不下发 thinking/reasoning 参数。"""
    monkeypatch.setenv("DEEPSEEK_API_BASE", GATEWAY)
    p = DeepSeekProvider(api_key="sk-test")
    assert p.base_url == GATEWAY
    assert p.supports_thinking is False
    assert p.supports_reasoning_roundtrip is False
    # 显式参数优先于环境变量
    p2 = DeepSeekProvider(api_key="sk-test", base_url="https://api.deepseek.com")
    assert p2.supports_thinking is True


def test_deepseek_gateway_key_paired_with_base(monkeypatch):
    """密钥与端点配对：走网关时网关专用 key 优先于宿主 shell 的官方 key。"""
    monkeypatch.setenv("DEEPSEEK_API_BASE", GATEWAY)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-official")
    monkeypatch.setenv("DEEPSEEK_GATEWAY_API_KEY", "sk-gateway")
    assert DeepSeekProvider().api_key == "sk-gateway"
    # 官方端点下仍用官方 key
    monkeypatch.delenv("DEEPSEEK_API_BASE")
    assert DeepSeekProvider().api_key == "sk-official"


def test_deepseek_thinking_flag_can_force_on(monkeypatch):
    """NOVELIST_DEEPSEEK_THINKING=1 时可对支持该字段的网关强制开启。"""
    monkeypatch.setenv("DEEPSEEK_API_BASE", GATEWAY)
    monkeypatch.setenv("NOVELIST_DEEPSEEK_THINKING", "1")
    assert DeepSeekProvider(api_key="sk-test").supports_thinking is True


def test_payload_omits_thinking_when_unsupported():
    """supports_thinking=False：thinking / reasoning_effort 全不下发，temperature 保留。"""
    p = build_payload(_req(thinking=True, temperature=0.4, reasoning_effort="high"),
                      "deepseek-v4-flash", supports_thinking=False)
    assert "thinking" not in p and "reasoning_effort" not in p
    assert p["temperature"] == 0.4


def test_create_passes_api_base_to_deepseek(monkeypatch):
    """--api-base（api_base 参数）能透传到 DeepSeek 适配器。"""
    from novelist.providers import create

    monkeypatch.delenv("DEEPSEEK_API_BASE", raising=False)
    p = create("deepseek", api_key="sk-test", api_base=GATEWAY)
    assert p.base_url == GATEWAY
    assert p.supports_thinking is False
