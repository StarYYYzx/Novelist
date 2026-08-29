"""配置加载与校验（docs/08 配置层）。

- pyproject/TOML 配置文件 -> dict -> Pydantic 校验（schemas/config.schema.json、policy.schema.json）。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field

from .core.errors import NovelistError


@dataclass
class ProviderConfig:
    primary: str = "openai"
    fallback: str | None = None
    model: str | None = None
    fallback_model: str | None = None
    embedding: str = "keyword-fallback"  # auto | cloud | local | keyword-fallback
    timeout_s: float = 60.0
    api_base: str | None = None


@dataclass
class SecurityConfig:
    policy_file: str = "policy.toml"
    sandbox_root: str | None = None
    moderation_precheck: bool = True


@dataclass
class BudgetConfig:
    default_max_tokens_out: int = 4000
    default_max_cost: float | None = None
    default_max_rounds: int = 100


@dataclass
class StorageConfig:
    use_indexdb: bool = True
    workspace_root: str | None = None


@dataclass
class Config:
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)


def load_config(path: str | None = None) -> Config:
    """加载 TOML 配置；缺失字段用默认值。"""
    if not path:
        return Config()
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        return Config()
    except tomllib.TOMLDecodeError as e:  # pragma: no cover - 配置错误
        raise NovelistError(f"config parse error: {e}")

    p = raw.get("provider", {})
    s = raw.get("security", {})
    b = raw.get("budget", {})
    st = raw.get("storage", {})
    return Config(
        provider=ProviderConfig(
            primary=p.get("primary", "openai"),
            fallback=p.get("fallback"),
            model=p.get("model"),
            fallback_model=p.get("fallback_model"),
            embedding=p.get("embedding", "keyword-fallback"),
            timeout_s=p.get("timeout_s", 60.0),
            api_base=p.get("api_base"),
        ),
        security=SecurityConfig(
            policy_file=s.get("policy_file", "policy.toml"),
            sandbox_root=s.get("sandbox_root"),
            moderation_precheck=s.get("moderation_precheck", True),
        ),
        budget=BudgetConfig(
            default_max_tokens_out=b.get("default_max_tokens_out", 4000),
            default_max_cost=b.get("default_max_cost"),
            default_max_rounds=b.get("default_max_rounds", 100),
        ),
        storage=StorageConfig(
            use_indexdb=st.get("use_indexdb", True),
            workspace_root=st.get("workspace_root"),
        ),
    )
