"""LLM Provider 单轨工厂测试（P0-2：providers.create 统一入口）。

覆盖 `src/novelist/providers/__init__.py` 的：
- REGISTRY 预设注册与查询；
- `create()` 对 fake/scripted 测试替身、命名预设、custom 的实例化；
- 命名预设默认 base_url / model / key_env 的应用与 api_base/model 覆盖；
- custom 必须显式 api_base；
- 未知名抛 KeyError；
- secrets.load_env_files / resolve_api_key 的 .env 与优先级行为。
"""

from __future__ import annotations

import os

import pytest

from novelist.providers import PRESETS, REGISTRY, create, get_provider, list_providers
from novelist.providers.deepseek import DeepSeekProvider
from novelist.providers.fake import FakeProvider, ScriptedProvider
from novelist.providers.openai import OpenAICompatibleProvider
from novelist.providers.secrets import load_env_files, resolve_api_key


# ---------------------------------------------------------------------------
# REGISTRY / 预设注册
# ---------------------------------------------------------------------------

def test_registry_has_all_presets_registered():
    expected = {"fake", "scripted", "deepseek", "openai", "qwen", "kimi",
                "glm", "anthropic", "ollama", "vllm", "custom"}
    assert expected <= set(REGISTRY)


def test_get_provider_returns_factory():
    assert callable(get_provider("deepseek"))
    assert get_provider("no-such") is None


def test_list_providers_is_sorted_and_contains():
    names = list_providers()
    assert names == sorted(names)
    assert "custom" in names and "deepseek" in names


def test_preset_defines_base_url_model_key_env():
    for name in ("openai", "qwen", "kimi", "glm", "anthropic", "ollama", "vllm"):
        p = PRESETS[name]
        assert p["base_url"].startswith("http"), p
        assert p["model"]
        assert p["key_env"]


# ---------------------------------------------------------------------------
# create()：测试替身
# ---------------------------------------------------------------------------

def test_create_fake_ignores_connection_kw():
    p = create("fake", api_key="ignored", api_base="ignored", model="ignored")
    assert isinstance(p, FakeProvider)


def test_create_scripted_demo_alias():
    script = [{"final": "done"}]
    assert isinstance(create("scripted", script=script), ScriptedProvider)
    assert isinstance(create("demo", script=script), ScriptedProvider)


# ---------------------------------------------------------------------------
# create()：命名预设
# ---------------------------------------------------------------------------

def test_create_deepseek_uses_default_model(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    p = create("deepseek")
    assert isinstance(p, DeepSeekProvider)
    assert p.model == "deepseek-v4-flash"


def test_create_openai_uses_given_base_and_model():
    p = create("openai", api_key="sk-x", api_base="https://gw.example/v1",
               model="gpt-4o-mini")
    assert isinstance(p, OpenAICompatibleProvider)
    assert p.base_url == "https://gw.example/v1"
    assert p.api_key == "sk-x"
    assert p.model == "gpt-4o-mini"


def test_create_qwen_applies_preset_defaults(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-q")
    p = create("qwen")
    assert p.base_url == PRESETS["qwen"]["base_url"]
    assert p.model == "qwen-plus"


def test_create_preset_api_base_overrides_default(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-q")
    p = create("qwen", api_base="https://relay.example/v1")
    assert p.base_url == "https://relay.example/v1"
    assert p.model == "qwen-plus"  # 模型默认不变


def test_create_preset_model_overrides_default(monkeypatch):
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-m")
    p = create("kimi", model="moonshot-v1-32k")
    assert p.model == "moonshot-v1-32k"


# ---------------------------------------------------------------------------
# create()：custom / 未知名
# ---------------------------------------------------------------------------

def test_create_custom_requires_api_base():
    with pytest.raises(ValueError):
        create("custom", api_key="sk-c")


def test_create_custom_resolves_fields():
    p = create("custom", api_key="sk-c", api_base="https://my.endpoint/v1",
               model="my-model")
    assert isinstance(p, OpenAICompatibleProvider)
    assert p.base_url == "https://my.endpoint/v1"
    assert p.api_key == "sk-c"
    assert p.model == "my-model"


def test_create_unknown_provider_raises_keyerror():
    with pytest.raises(KeyError):
        create("no-such-provider")


# ---------------------------------------------------------------------------
# secrets：/.env 加载与优先级
# ---------------------------------------------------------------------------

def test_load_env_files_injects_from_file(tmp_path):
    env_file = tmp_path / "env_test"
    env_file.write_text(
        "# comment line\nFOO_KEY=sk-from-file\nEMPTY_LINE\nBAR = 'quoted-value'\n",
        encoding="utf-8",
    )
    load_env_files([str(env_file)])
    assert os.environ.get("FOO_KEY") == "sk-from-file"
    assert os.environ.get("BAR") == "quoted-value"


def test_load_env_files_does_not_override_existing(monkeypatch, tmp_path):
    monkeypatch.setenv("FOO_KEY", "already-set")
    env_file = tmp_path / "no_override.env"
    env_file.write_text("FOO_KEY=sk-from-file\n", encoding="utf-8")
    load_env_files([str(env_file)])
    assert os.environ["FOO_KEY"] == "already-set"


def test_load_env_files_missing_returns_none():
    assert load_env_files([r"Z:\no\such\path\missing.env"]) is None


def test_resolve_api_key_explicit_beats_env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env")
    assert resolve_api_key("sk-cli", ["DEEPSEEK_API_KEY"]) == "sk-cli"


def test_resolve_api_key_empty_env_list_returns_none(monkeypatch):
    assert resolve_api_key(None, []) is None