"""DeepSeek 适配器（docs/07 §2.4）。

DeepSeek 提供 OpenAI 兼容的 chat/completions 协议。本适配器是 OpenAICompatibleProvider
的 DeepSeek 特化：

- base_url = https://api.deepseek.com
- 默认模型 `deepseek-v4-flash`（官方 v4 系列；旧的 deepseek-chat/reasoner 已由 v4 取代）
- **用户规则（2026-09-06）：全局禁用 `-pro`，只允许 flash**——pro 思考模式下超大池
  judge 任务推理 token 波动致 content 常空（见 docs/问题总账 B1），且成本高。
- api_key 从环境变量 **`DEEPSEEK_API_KEY`**（优先）或旧名 `DeepSeek-API-KEY` 读取，
  **永不写入配置文件 / 工作区**（隐私见 providers/secrets.py）
- 思考模式按请求开关（`thinking`），工具多轮回传 `reasoning_content`
  （`supports_reasoning_roundtrip=True`），否则 DeepSeek 返回 400。
"""

from __future__ import annotations


from .openai import OpenAICompatibleProvider, parse_completion
from .secrets import resolve_api_key

# 全局只用 flash（用户规则：禁用 pro）。如需显式其它模型可传 `model=` 覆盖。
DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-flash"


class DeepSeekProvider(OpenAICompatibleProvider):
    """DeepSeek LLM 适配器（OpenAI 兼容协议，v4 系列，支持按请求思考）。

    端点优先级（2026-09-19 增加网关支持）：显式 `base_url` 参数 >
    环境变量 `DEEPSEEK_API_BASE`（可在 gitignored 的 `.env` 里设，用于接校内/中转
    OpenAI 兼容网关）> 官方 `https://api.deepseek.com`。协议完全一致，仅端点不同。
    """

    DEEPSEEK_BASE = "https://api.deepseek.com"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        timeout_s: float | None = None,
        base_url: str | None = None,
    ) -> None:
        import os as _os

        base = (base_url or _os.environ.get("DEEPSEEK_API_BASE") or self.DEEPSEEK_BASE)
        _official = base.rstrip("/") == self.DEEPSEEK_BASE
        # 密钥与端点配对（2026-09-19）：走第三方/校内网关时，官方 key 无效；
        # 网关专用 key 变量优先，避免"宿主 shell 里的官方 DEEPSEEK_API_KEY 盖住
        # .env 里的网关 key"（环境变量 > .env 的既定优先级所致）。
        key_names = ["DEEPSEEK_API_KEY", "DeepSeek-API-KEY"]
        if not _official:
            key_names = ["DEEPSEEK_GATEWAY_API_KEY", *key_names]
        key = resolve_api_key(api_key, key_names)
        # 思考字段：官方端点支持；第三方/校内网关（base 非官方）默认不下发
        # （litellm 类代理未开该字段 → 400）。可用 NOVELIST_DEEPSEEK_THINKING=1/0 覆盖。
        _flag = _os.environ.get("NOVELIST_DEEPSEEK_THINKING")
        supports_thinking = _official if _flag is None else _flag not in ("0", "false", "False")
        super().__init__(
            base_url=base,
            api_key=key,
            model=model or DEEPSEEK_DEFAULT_MODEL,
            timeout_s=timeout_s,
            supports_reasoning_roundtrip=_official,
            supports_thinking=supports_thinking,
        )
        self.provider_name = "deepseek"

    def complete(self, req):
        res = super().complete(req)
        res.provider = "deepseek"  # 统一 provider 归属（含思考型响应）
        return res


def parse_deepseek(status_code: int, data: dict):
    """解析 DeepSeek 响应为标准 LLMResult（复用 OpenAI 兼容解析）。"""
    res = parse_completion(status_code, data)
    res.provider = "deepseek"
    return res