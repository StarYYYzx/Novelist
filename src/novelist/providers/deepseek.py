"""DeepSeek 适配器（docs/07 §2.4）。

DeepSeek 提供 OpenAI 兼容的 chat/completions 协议。本适配器是 OpenAICompatibleProvider
的 DeepSeek 特化：base_url = https://api.deepseek.com，api_key 从环境变量 `DeepSeek-API-KEY` 读取。
"""

from __future__ import annotations

import os

from .openai import OpenAICompatibleProvider, parse_completion


class DeepSeekProvider(OpenAICompatibleProvider):
    """DeepSeek LLM 适配器（OpenAI 兼容协议）。"""

    DEEPSEEK_BASE = "https://api.deepseek.com"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "deepseek-chat",
        timeout_s: float = 60.0,
    ) -> None:
        key = api_key or os.getenv("DeepSeek-API-KEY", "")
        super().__init__(
            base_url=self.DEEPSEEK_BASE,
            api_key=key,
            model=model,
            timeout_s=timeout_s,
        )
        self.provider_name = "deepseek"


def parse_deepseek(status_code: int, data: dict):
    """解析 DeepSeek 响应为标准 LLMResult（复用 OpenAI 兼容解析）。"""
    res = parse_completion(status_code, data)
    res.provider = "deepseek"
    return res
