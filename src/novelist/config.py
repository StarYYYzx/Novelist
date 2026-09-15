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
    embedding: str = "auto"  # auto=内置local优先 | cloud | local | keyword-fallback
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
    # 正文预算（第二批·人工审查）：期望正文输出量；适配层保证总预算至少覆盖它。
    # 总预算（default_max_tokens_out）是硬顶，正文预算决定"该写多少"。
    default_max_content_tokens: int = 3000
    default_max_cost: float | None = None
    default_max_rounds: int = 100


@dataclass
class StorageConfig:
    use_indexdb: bool = True
    workspace_root: str | None = None


# U7（2026-09-15 拍板）：chapter 管线的项目级默认值（config.toml `[generation]` 段）。
# 全字段默认 None = 未配置 —— **显式 CLI flag > 配置 > 出厂默认**（resolve_opt），
# 无配置文件时行为与历史版本逐字节一致（不改任何默认值）。
# 仅收管线开关与预算；provider/api-key/policy 这类"每次调用都可能不同"的参数
# 不进配置，保持 CLI 专属。
GENERATION_KEYS = (
    "gen_tokens", "content_tokens", "max_events", "min_event_words",
    "inject_bible", "polish", "event_loop", "screenplay", "readback",
    "event_polish", "supplement_settings", "jit_characters",
    "seam_review", "volume_facts",
    "agentic_chronicle", "agentic_review",
    "agentic_chronicle_rounds", "agentic_review_rounds",
)


@dataclass
class GenerationConfig:
    gen_tokens: int | None = None
    content_tokens: int | None = None
    max_events: int | None = None
    min_event_words: int | None = None
    inject_bible: bool | None = None
    polish: bool | None = None
    event_loop: bool | None = None
    screenplay: bool | None = None
    readback: bool | None = None
    event_polish: bool | None = None
    supplement_settings: bool | None = None
    jit_characters: bool | None = None
    seam_review: bool | None = None
    volume_facts: bool | None = None
    agentic_chronicle: bool | None = None
    agentic_review: bool | None = None
    agentic_chronicle_rounds: int | None = None
    agentic_review_rounds: int | None = None


@dataclass
class Config:
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    generation: GenerationConfig = field(default_factory=GenerationConfig)


def resolve_opt(explicit, configured, default):
    """三级优先级解析（U7）：显式 CLI 值 > 配置文件值 > 出厂默认。"""
    if explicit is not None:
        return explicit
    return configured if configured is not None else default


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
    g = raw.get("generation", {})
    unknown = sorted(set(g) - set(GENERATION_KEYS)) if isinstance(g, dict) else []
    if unknown:
        # 拼错键静默失效比报错更糟（"配置了 polish 却不生效"会浪费一次真机生成）
        raise NovelistError(f"config [generation] 存在未知键：{', '.join(unknown)}"
                            f"（合法键：{', '.join(GENERATION_KEYS)}）")
    return Config(
        provider=ProviderConfig(
            primary=p.get("primary", "openai"),
            fallback=p.get("fallback"),
            model=p.get("model"),
            fallback_model=p.get("fallback_model"),
            embedding=p.get("embedding", "auto"),
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
            default_max_content_tokens=b.get("default_max_content_tokens", 3000),
            default_max_cost=b.get("default_max_cost"),
            default_max_rounds=b.get("default_max_rounds", 100),
        ),
        storage=StorageConfig(
            use_indexdb=st.get("use_indexdb", True),
            workspace_root=st.get("workspace_root"),
        ),
        generation=GenerationConfig(**{k: g[k] for k in GENERATION_KEYS if k in g}),
    )
