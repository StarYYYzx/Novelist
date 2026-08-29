"""Embedding 抽象与降级实现（docs/07 §2.1.1 / §8，F9.4）。

设计约束（F9.4）：**检索能力必须具备降级路径**——无 Embedding Provider 时退化为关键词索引，
仍可用。两条路径同接口（`EmbeddingProvider` Protocol，见 core/llm.py），检索层无分支。

- `KeywordEmbedding`：默认降级实现。中文字符二元组 + 哈希投影到定长向量，L2 归一化。
  确定性（用 blake2b，不用内置 hash——后者对 str 有进程级随机盐）、零依赖、零网络、可离线。
  对中文子串/近义片段有合理的模糊匹配能力，优于朴素子串包含判断。
- `OpenAIEmbedding`：真实向量（OpenAI 兼容 /embeddings）。可选依赖 httpx，无 key 时抛 ProviderError
  由上层降级到 KeywordEmbedding。
"""

from __future__ import annotations

import hashlib
import math
import re

from .errors import ProviderError

KEYWORD_KIND = "keyword-hash"
DEFAULT_DIM = 256

_CJK = r"\u4e00-\u9fff\u3400-\u4dbf"
_CJK_RUN = re.compile(f"[{_CJK}]+")
_WORD_RUN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """把文本切成检索单元：中文按字符二元组，英文/数字按词 + 字符二元组。"""
    t = (text or "").lower()
    toks: list[str] = []
    for seg in _CJK_RUN.findall(t):
        if len(seg) == 1:
            toks.append(seg)
        else:
            toks.extend(seg[i : i + 2] for i in range(len(seg) - 1))
    for seg in _WORD_RUN.findall(t):
        toks.append(seg)
        if len(seg) > 1:
            toks.extend(seg[i : i + 2] for i in range(len(seg) - 1))
    return toks


def _bucket(token: str, dim: int) -> int:
    """确定性哈希分桶（跨进程稳定）。"""
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % dim


def _l2(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return vec
    return [v / norm for v in vec]


def cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度；维度不一致或空向量返回 0.0（不抛错，检索层按"未命中"处理）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class KeywordEmbedding:
    """关键词降级向量（docs/07 §8 降级总表）。确定性、零依赖。"""

    kind = KEYWORD_KIND

    def __init__(self, dim: int = DEFAULT_DIM) -> None:
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        """哈希投影向量。注意：定长哈希存在碰撞，短文本下碰撞噪声可能盖过真实信号，
        因此**检索打分不走余弦**——`MemoryRetriever` 在关键词模式下改用精确 token 打分
        （见 core/memory.py）。本方法保留是为了满足 `EmbeddingProvider` Protocol、
        并提供 `kind`/`dim` 供降级判定与可观测（检索结果里的 `mode` 字段）。
        """
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self._dim
            for tok in tokenize(text):
                vec[_bucket(tok, self._dim)] += 1.0
            out.append(_l2(vec))
        return out


class OpenAIEmbedding:
    """OpenAI 兼容 /embeddings 适配器（可选依赖 httpx）。"""

    kind = "openai"

    _KNOWN_DIM = {
        "text-embedding-3-small": 1536,
        "text-embedding-3-large": 3072,
        "text-embedding-ada-002": 1536,
    }

    def __init__(
        self,
        *,
        model: str = "text-embedding-3-small",
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_s: float = 30.0,
        dim: int | None = None,
    ) -> None:
        import os

        try:
            import httpx  # noqa: F401  # type: ignore
        except ImportError as e:  # pragma: no cover - 可选依赖
            raise ProviderError("httpx not installed (pip install novelist[providers])") from e
        self.model = model
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.base_url = (base_url or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.timeout_s = timeout_s
        self._dim = dim or self._KNOWN_DIM.get(model, 1536)

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx  # type: ignore

        if not self.api_key:
            raise ProviderError("no api key for embedding; set OPENAI_API_KEY")
        payload = {"model": self.model, "input": list(texts)}
        try:
            resp = httpx.post(
                f"{self.base_url}/embeddings",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout_s,
            )
        except httpx.HTTPError as e:  # type: ignore
            raise ProviderError(f"embedding request failed: {e}") from e
        if resp.status_code != 200:
            raise ProviderError(f"embedding http {resp.status_code}: {resp.text[:200]}")
        data = resp.json().get("data") or []
        # /embeddings 不保证返回顺序与 input 一致，按 index 归位
        ordered: list[list[float]] = [[] for _ in texts]
        for item in data:
            idx = int(item.get("index", -1))
            if 0 <= idx < len(ordered):
                ordered[idx] = [float(x) for x in item.get("embedding") or []]
        if ordered and all(len(v) == len(ordered[0]) for v in ordered):
            self._dim = len(ordered[0])
        return ordered


def make_embedding(spec: str | None = None, **kw):
    """按配置名构造 Embedding Provider（docs/08 配置层 provider.embedding）。

    - None / "" / "keyword" / "keyword-fallback" / "auto"（无 key 时）→ KeywordEmbedding
    - "openai" → OpenAIEmbedding；构造失败（缺 key/缺 httpx）自动降级为 KeywordEmbedding
    """
    name = (spec or "keyword-fallback").lower()
    if name in ("keyword", "keyword-fallback", "none", "off"):
        return KeywordEmbedding(**kw)
    if name in ("openai", "cloud"):
        import os

        if not (kw.get("api_key") or os.getenv("OPENAI_API_KEY")):
            return KeywordEmbedding()  # 无凭据 → 降级，不抛错
        try:
            return OpenAIEmbedding(**kw)
        except ProviderError:
            return KeywordEmbedding()  # 缺 httpx 等 → 同样降级
    return KeywordEmbedding()
