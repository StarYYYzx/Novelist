"""LLM Provider 适配器插件（docs/07 §2.3/§2.4）。可选依赖按需加载。

注册表：`name -> 工厂可调用`；实际实例化在核心层（config 指定 primary/fallback）。
"""

from __future__ import annotations

from typing import Any, Protocol


class ProviderFactory(Protocol):
    def __call__(self, **kw: Any):
        ...


REGISTRY: dict[str, callable] = {}


def register_provider(name: str, factory) -> None:
    REGISTRY[name] = factory


def get_provider(name: str):
    return REGISTRY.get(name)


# 内置注册：
# - openai：OpenAI 兼容协议（真实，需 key/base_url）
# - fake：确定性测试用（不走网络）
def _make_openai(**kw):
    from .openai import OpenAICompatibleProvider

    return OpenAICompatibleProvider(**kw)


def _make_deepseek(**kw):
    from .deepseek import DeepSeekProvider

    return DeepSeekProvider(**kw)


def _make_fake(**kw):
    from .fake import FakeProvider

    return FakeProvider(**kw)


def _make_lmstudio(**kw):
    from .lmstudio import LMStudioProvider

    return LMStudioProvider(**kw)


register_provider("openai", _make_openai)
register_provider("deepseek", _make_deepseek)
register_provider("fake", _make_fake)
register_provider("lmstudio", _make_lmstudio)
