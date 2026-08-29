"""LLM Provider 适配器插件（docs/07 §2.3）。可选依赖按需加载。"""

from __future__ import annotations

from ..core.llm import EmbeddingProvider, LLMProvider


REGISTRY: dict[str, type[LLMProvider]] = {}


def register_provider(name: str, cls: type[LLMProvider]) -> None:
    REGISTRY[name] = cls


def get_provider(name: str) -> type[LLMProvider] | None:
    return REGISTRY.get(name)
