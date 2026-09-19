"""LLM Provider 适配器插件（docs/07 §2.3/§2.4）。可选依赖按需加载。

单轨注册：`name -> 工厂可调用`，实例化统一走 `providers.create(name, **kw)`（P0-2 归一）。
新增 provider = 实现 LLMProvider 协议 + 在此注册工厂（唯一入口，禁在 cli 加 if/elif）。

内置预设（docs/07 §2.4）：
- `fake` / `scripted`：确定性测试替身（不走网络，docs/09 §2.1）；
- `deepseek`：DeepSeek API（v4-flash，禁 pro，思考型）；
- `openai`、`qwen`、`kimi`、`glm`、`anthropic`：主流云 API（OpenAI 兼容协议）；
- `ollama` / `vllm`：本地推理（OpenAI 兼容网关）；
- `custom`：任意 OpenAI 兼容端点，用户自配 `--api-base/--api-key/--model`。

Key 来源（providers/secrets.py）：CLI 显式参数 > 环境变量 > 本机 `.env`（gitignored）。
"""

from __future__ import annotations

from typing import Any

from .secrets import load_env_files, resolve_api_key


class ProviderFactory:
    def __call__(self, **kw: Any):
        ...


REGISTRY: dict[str, Any] = {}


def register_provider(name: str, factory) -> None:
    REGISTRY[name] = factory


def get_provider(name: str):
    return REGISTRY.get(name)


def list_providers() -> list[str]:
    """可用 provider 名（预设 + 别名）。"""
    return sorted(REGISTRY)


# ---- 内置预设注册（P0-2：单一入口，全部实例化走 create()） ----
def _make_fake(**kw):
    from .fake import FakeProvider

    return FakeProvider(**kw)


def _make_scripted(**kw):
    from .fake import ScriptedProvider

    return ScriptedProvider(**kw)


def _make_deepseek(**kw):
    from .deepseek import DeepSeekProvider

    return DeepSeekProvider(**kw)


def _make_openai_compat(**kw):
    """OpenAI 兼容适配器工厂（openai/qwen/kimi/glm/anthropic/ollama/vllm/custom 通用）。"""
    from .openai import OpenAICompatibleProvider

    return OpenAICompatibleProvider(**kw)


register_provider("fake", _make_fake)
register_provider("scripted", _make_scripted)
register_provider("deepseek", _make_deepseek)
register_provider("openai", _make_openai_compat)
register_provider("qwen", _make_openai_compat)
register_provider("kimi", _make_openai_compat)
register_provider("glm", _make_openai_compat)
register_provider("anthropic", _make_openai_compat)
register_provider("ollama", _make_openai_compat)
register_provider("vllm", _make_openai_compat)
register_provider("custom", _make_openai_compat)

# 各命名预设的默认接入信息（docs/07 §2.4；custom 需用户显式给定，见 create()）。
# key_env：按优先级排列的环境变量名（.env 文件亦可注入，见 secrets.load_env_files）。
PRESETS: dict[str, dict[str, Any]] = {
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini",
               "key_env": ["OPENAI_API_KEY"]},
    "qwen": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus",
             "key_env": ["DASHSCOPE_API_KEY", "QWEN_API_KEY"]},
    "kimi": {"base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k",
             "key_env": ["MOONSHOT_API_KEY", "KIMI_API_KEY"]},
    "glm": {"base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash",
            "key_env": ["ZHIPU_API_KEY", "GLM_API_KEY"]},
    "anthropic": {"base_url": "https://api.anthropic.com", "model": "claude-3-5-sonnet",
                  "key_env": ["ANTHROPIC_API_KEY"]},
    "ollama": {"base_url": "http://localhost:11434/v1", "model": "qwen2.5:7b",
               "key_env": ["OLLAMA_API_KEY"]},
    "vllm": {"base_url": "http://localhost:8000/v1", "model": "Qwen/Qwen2.5-7B-Instruct",
             "key_env": ["VLLM_API_KEY"]},
}


def create(
    name: str = "fake",
    *,
    api_key: str | None = None,
    api_base: str | None = None,
    model: str | None = None,
    vol: int = 1,
    ch: int = 1,
    **kw: Any,
):
    """统一 LLM Provider 工厂（P0-2）：按名查 REGISTRY 实例化，Key 统一解析。

    - `fake` / `scripted`：测试替身，key/base/model 忽略（vol/ch 供占位/脚本）。
    - 命名预设（deepseek/openai/qwen/kimi/glm/anthropic/ollama/vllm）：api_base 可覆盖
      内置默认值（如接中转网关）；api_key 不传则回退对应 key_env 或 .env。
    - `custom`：必须显式给 `api_base`（或 `model`），api_key 可省略（回退 key_env/.env ——
      通常由用户 `--api-base/--api-key/--model` 全量自定义任意 OpenAI 兼容端点）。
    未知名抛 KeyError。
    """
    load_env_files()
    name = name or "fake"

    if name == "fake":
        return _make_fake(**kw)
    if name in ("scripted", "demo"):
        return _make_scripted(**kw)

    factory = REGISTRY.get(name)
    if factory is None:
        raise KeyError(
            f"unknown LLM provider {name!r}; available: {list_providers()} (+'custom')"
        )

    if name == "deepseek":
        return factor_factory_provider(name, factory, api_key=api_key, model=model,
                                       base_url=api_base, **kw)

    preset = PRESETS.get(name, {})
    if api_base is None and name == "custom":
        raise ValueError(
            "custom provider 需要显式 --api-base（OpenAI 兼容端点 URL）"
            "（可选 --api-key/--model）"
        )
    resolved_base = api_base or preset.get("base_url")
    resolved_model = model or preset.get("model")
    env_names = preset.get("key_env", [])
    resolved_key = resolve_api_key(api_key, env_names)
    return factory(
        base_url=resolved_base,
        api_key=resolved_key,
        model=resolved_model,
        **kw,
    )


def factor_factory_provider(name: str, factory, *, api_key=None, model=None,
                            base_url=None, **kw):
    """DeepSeek：走特化适配器（思考型 / 禁 pro / reasoning roundtrip）。

    `base_url` 可覆盖端点（2026-09-19）：显式 `--api-base` > 环境变量
    `DEEPSEEK_API_BASE`（.env）> 官方端点；协议一致，仅网关不同。
    """
    return factory(api_key=api_key, model=model, base_url=base_url, **kw)