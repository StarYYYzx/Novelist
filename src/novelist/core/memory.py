"""记忆子系统（docs/04 §5.8 / docs/07 §7）。

分两级：global（long-term，沉淀到 memory/ 工作区 + RAG）+ context（当前会话工作记忆）。
读取接口面向所有 Agent；写入接口默认仅授予主编剧/编纂员。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MemoryHit:
    sig: str
    kind: str  # experience | plot_event | relationship | thread
    text: str
    source: dict  # {vol, ch}
    refs: list[str]
    score: float


@dataclass
class MemoryQuery:
    query: str
    filters: dict | None = None  # char_id / chapter_scope / after / kinds
    top_k: int = 5
    min_score: float = 0.0


class MemoryRetriever:
    """检索接口（docs/07 §7.1）。

    语义检索经 EmbeddingProvider；无 embedding 时由调用方/实现降级为关键词检索（docs/07 §8）。
    """

    def query(self, q: MemoryQuery) -> list[MemoryHit]:
        raise NotImplementedError  # pragma: no cover - 脚手架桩


class MemoryWriter:
    """写入接口（sensitive，编纂员/主编剧）（docs/07 §7.2）。"""

    def append_experience(self, char_id: str, entry: dict) -> None:
        raise NotImplementedError  # pragma: no cover

    def append_plot_event(self, event: dict) -> None:
        raise NotImplementedError  # pragma: no cover

    def record_relationship_change(self, a: str, b: str, event: dict) -> None:
        raise NotImplementedError  # pragma: no cover
