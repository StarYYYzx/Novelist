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

import os

from .openai import OpenAICompatibleProvider, parse_completion
from .secrets import resolve_api_key

# 全局只用 flash（用户规则：禁用 pro）。如需显式其它模型可传 `model=` 覆盖。
DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-flash"


class DeepSeekProvider(OpenAICompatibleProvider):
    """DeepSeek LLM 适配器（OpenAI 兼容协议，v4 系列，支持按请求思考）。"""

    DEEPSEEK_BASE = "https://api.deepseek.com"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        key = resolve_api_key(
            api_key,
            ["DEEPSEEK_API_KEY", "DeepSeek-API-KEY"],
        )
        super().__init__(
            base_url=self.DEEPSEEK_BASE,
            api_key=key,
            model=model or DEEPSEEK_DEFAULT_MODEL,
            timeout_s=timeout_s,
            supports_reasoning_roundtrip=True,
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