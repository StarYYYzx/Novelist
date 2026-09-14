"""Embedding 抽象与降级实现（docs/07 §2.1.1 / §8，F9.4）。

设计约束（F9.4）：**检索能力必须具备降级路径**——无 Embedding Provider 时退化为关键词索引，
仍可用。两条路径同接口（`EmbeddingProvider` Protocol，见 core/llm.py），检索层无分支。

- `KeywordEmbedding`：默认降级实现。中文字符二元组 + 哈希投影到定长向量，L2 归一化。
  确定性（用 blake2b，不用内置 hash——后者对 str 有进程级随机盐）、零依赖、零网络、可离线。
  对中文子串/近义片段有合理的模糊匹配能力，优于朴素子串包含判断。
- `OpenAIEmbedding`：真实向量（OpenAI 兼容 /embeddings）。可选依赖 httpx，无 key 时抛 ProviderError
  由上层降级到 KeywordEmbedding。
- `LocalEmbedding`：项目内置真实向量（fastembed/ONNX，nomic-embed-text-v1.5，CPU 纯离线）。
  可选依赖 fastembed：**缺依赖由 make_embedding 在构造期降级**；
  **模型加载/推理失败由本类在运行期就地降级**（P-FE2，2026-09-15）——
  构造期探针只查 `import fastembed`，真正加载发生在首次 `embed()`，此时失败原先会
  抛 `ProviderError` 穿透到上层（CLI 崩溃 / 记忆层静默为空），现改为委托 KeywordEmbedding，
  且 `kind` 随之变为 `keyword-hash`，检索层自动切到精确 token 打分。
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import time

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


def default_cache_dir() -> str:
    """fastembed 权重缓存目录（持久位，P-FE1）。

    2026-09-15 现实验证：fastembed 默认把权重缓存到进程临时目录（`%TEMP%`），
    系统清理临时目录即整个模型消失，下次运行触发重新下载；在网络受限环境下
    直接退化为关键词模式（本机实测缓存目录 0 条目 + 直连超时）。
    改为持久位，优先级：`NOVELIST_EMBED_CACHE` > 平台缓存目录 > `~/.cache`。
    """
    env = os.environ.get("NOVELIST_EMBED_CACHE")
    if env:
        return env
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME")
    if base:
        return os.path.join(base, "novelist", "fastembed")
    return os.path.join(os.path.expanduser("~"), ".cache", "novelist", "fastembed")


def _load_attempts() -> int:
    """模型加载尝试次数（默认 2：一次退避重试吸收瞬时网络抖动）。"""
    try:
        return max(1, int(os.environ.get("NOVELIST_EMBED_LOAD_ATTEMPTS") or "2"))
    except (TypeError, ValueError):
        return 2


# 进程级加载失败记忆：一次失败后，同一进程内后续实例**立即**降级，不再重复付
# 连接超时的代价（实测：不记忆时一次章节生成会为同一失败付多次 ~20s 超时）。
# 只影响当前进程；新进程会重新尝试（网络恢复后自然恢复真实向量）。
_LOAD_FAILURE: str = ""


class LocalEmbedding:
    """项目内置向量后端（fastembed/ONNX，CPU，零 torch 零网络）。

    可选依赖 fastembed（pip install novelist[embedding]）：首次调用自动下载
    nomic-embed-text-v1.5 ONNX 权重到**持久缓存目录**（可再生，ADR-016 语义）。
    缺依赖由 make_embedding 在构造期降级；**模型加载/推理失败在运行期就地降级**
    （见模块 docstring 的 P-FE2 说明）。

    注意：不加 nomic 官方 "search_document:"/"search_query:" 前缀——与
    LM-Studio /v1/embeddings 行为保持一致（对话历史同款向量语义）。
    """

    _KIND = "local"
    DEFAULT_MODEL = "nomic-ai/nomic-embed-text-v1.5"
    _KNOWN_DIM = {"nomic-ai/nomic-embed-text-v1.5": 768}

    def __init__(self, *, model: str = DEFAULT_MODEL, cache_dir: str | None = None) -> None:
        self.model = model
        self._cache_dir = cache_dir or default_cache_dir()
        self._dim = self._KNOWN_DIM.get(model, 768)
        self._backend = None  # 惰性加载：首次 embed 才初始化（导入快，测试零依赖）
        # 运行期降级状态（P-FE2）。用 `_degrade_reason` 是否为空表达"是否已降级"——
        # 单一事实源，避免两个布尔标志互相漂移。
        self._fallback = KeywordEmbedding()
        self._degrade_reason = ""

    @property
    def kind(self) -> str:
        """降级后返回 `keyword-hash`，检索层据此切到精确 token 打分（不是类属性）。"""
        return KEYWORD_KIND if self._degrade_reason else self._KIND

    @property
    def degraded(self) -> bool:
        return bool(self._degrade_reason)

    @property
    def degrade_reason(self) -> str:
        """降级原因（空串 = 未降级）。供上层写进 soft_failures / 日志，不静默。"""
        return self._degrade_reason

    def _degrade(self, err: object) -> None:
        if not self._degrade_reason:
            self._degrade_reason = f"{type(err).__name__}: {err}"
            self._dim = self._fallback.dim

    def _ensure_backend(self):
        global _LOAD_FAILURE
        if self._backend is not None:
            return self._backend
        if _LOAD_FAILURE:
            # 本进程已失败过 → 不再重试（避免为同一次网络故障反复付超时）
            raise ProviderError(_LOAD_FAILURE)
        try:
            # 国内网络直连 huggingface.co 普遍 502（实测 2026-09-04）；hf-mirror 为全量代理，
            # 海外亦可用。用户显式设置 HF_ENDPOINT 时不覆盖（setdefault 语义）。
            os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
            # Windows 无开发者模式时 huggingface_hub 会打印 symlink 警告（不影响功能，
            # 只是缓存退化为复制）。纯静音，不改行为。
            os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
            from fastembed import TextEmbedding
        except ImportError as e:  # pragma: no cover - 可选依赖
            raise ProviderError(
                "fastembed not installed (pip install novelist[embedding])") from e
        attempts = _load_attempts()
        last: Exception | None = None
        for i in range(attempts):
            if i:
                time.sleep(min(0.5 * i, 2.0))  # 退避：只吸收瞬时抖动，不做长等待
            try:
                os.makedirs(self._cache_dir, exist_ok=True)
                self._backend = TextEmbedding(model_name=self.model, cache_dir=self._cache_dir)
                return self._backend
            except Exception as e:  # noqa: BLE001 - 下载失败/权重损坏等一律重试后走降级
                last = e
        _LOAD_FAILURE = f"fastembed load failed: {last}"
        raise ProviderError(_LOAD_FAILURE) from last

    @property
    def dim(self) -> int:
        return self._dim

    def cached(self) -> bool:
        """权重是否已在本地缓存（只查目录，不做网络探测）。

        用途：区分"本地已有权重，直接加载"与"需要联网下载"——测试与上层据此决定
        是否值得发起下载（CI/离线环境不应因为一次向量测试就拉几百 MB）。
        """
        path = os.path.join(self._cache_dir, f"models--{self.model.replace('/', '--')}")
        try:
            return os.path.isdir(path) and bool(os.listdir(path))
        except OSError:
            return False

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self._degrade_reason:
            return self._fallback.embed(texts)
        try:
            be = self._ensure_backend()
            out = [[float(x) for x in vec] for vec in be.embed(texts)]
        except Exception as e:  # noqa: BLE001 - 运行期降级（P-FE2 / F9.4）：不回抛，不静默
            self._degrade(e)
            return self._fallback.embed(texts)
        if out and out[0]:
            self._dim = len(out[0])
        return out


def make_embedding(spec: str | None = None, **kw):
    """按配置名构造 Embedding Provider（docs/08 配置层 provider.embedding）。

    - None / "" / "keyword" / "keyword-fallback" / "none" / "off" → KeywordEmbedding
    - "openai" → OpenAIEmbedding；构造失败（缺 key/缺 httpx）自动降级为 KeywordEmbedding
    - "local" / "fastembed" → LocalEmbedding（项目内置 ONNX，CPU）；
      缺 fastembed 依赖在此处降级 KeywordEmbedding；模型加载失败不在构造期暴露，
      改由 LocalEmbedding 首次 embed() 时就地降级（P-FE2）
    - "auto"（默认）→ **内置模型优先**（用户需求：所有 embedding 任务统一走系统自带模型）：
      local → openai（有 key 时）→ keyword
    """
    name = (spec or "auto").lower()
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
    if name in ("local", "fastembed"):
        try:
            import fastembed  # noqa: F401  # 构造期探针：LocalEmbedding 本体惰性加载，
        except ImportError:     # 缺依赖必须在这里拦下，否则降级链失效
            return KeywordEmbedding()
        try:
            return LocalEmbedding(**kw)
        except Exception:  # noqa: BLE001 - 模型失败等 → 降级（F9.4）
            return KeywordEmbedding()
    if name == "auto":
        # 内置 ONNX 模型优先（离线、零 API 成本、768 维真实语义向量）；
        # 本地不可用再试云端（需 key），最后关键词降级。
        try:
            import fastembed  # noqa: F401
        except ImportError:
            pass
        else:
            try:
                return LocalEmbedding()
            except Exception:  # noqa: BLE001
                pass
        import os

        if kw.get("api_key") or os.getenv("OPENAI_API_KEY"):
            try:
                return OpenAIEmbedding(**kw)
            except ProviderError:
                pass
        return KeywordEmbedding()
    return KeywordEmbedding()
