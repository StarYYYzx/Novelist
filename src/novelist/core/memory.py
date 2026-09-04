"""记忆子系统（docs/04 §5.8 / docs/06 §3.5 / docs/07 §7，ADR-011，M3 落地）。

分两级：global（long-term，沉淀到 memory/ 工作区 + RAG 索引）+ context（当前会话工作记忆）。
读取接口面向所有 Agent；写入接口默认仅授予主编剧/编纂员。

本模块提供三件事：

1. `MemoryIndex` —— 记忆碎片索引。从 `memory/` 事实源收割碎片（剧情事件/人物经历/关系变化），
   落盘到 `memory/fragment_index.json`；向量存 `memory/rag/vectors.json`（可再生，可删）。
2. `MemoryRetriever` —— 检索（docs/07 §7.1）。语义 + 关键词双通道**同一套余弦打分**，
   差别只在 Embedding Provider：有真实 embedding 走语义，无则退化为 `KeywordEmbedding`
   （F9.4 降级路径，接口完全一致，检索层无分支）。
3. `MemoryWriter` —— 写入（sensitive，docs/07 §7.2）。写前做**冲突双检**：
   规则层（重复入库、bible 引用完整性）在本模块确定性完成；
   语义层由 `semantic_checker` 回调注入（编纂员子代理 / LLM，07§7.2 的双检第二层）。

冲突即抛 `MemoryConflictError`（docs/06 §4.4 `conflicted` → 人工仲裁），**不静默入库**。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import math

from .embedding import KEYWORD_KIND, KeywordEmbedding, cosine, tokenize

# bible 前缀 -> 工作区文件（引用完整性校验用，docs/06 §5.2）
_BIBLE_SOURCES = {
    "char:": "bible/characters.json",
    "pt:": "bible/plot_threads.json",
    "loc:": "bible/locations.json",
}


class MemoryConflictError(Exception):
    """新增记忆与既有记忆/bible 冲突（docs/06 §4.4 contradicted）。

    抛出即回退，不静默入库；由编排层提请人工仲裁（UC-17）。
    """


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
    filters: dict | None = None  # char_id / chapter_scope / after / before / kinds
    top_k: int = 5
    min_score: float = 0.0


@dataclass
class MemoryFragment:
    """一条可检索的记忆碎片（docs/06 §3.5 fragment_index）。"""

    sig: str
    kind: str
    text: str
    source: dict          # {vol, ch}
    refs: list[str] = field(default_factory=list)
    hash: str = ""
    char_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "sig": self.sig,
            "kind": self.kind,
            "text": self.text,
            "source": self.source,
            "refs": self.refs,
            "hash": self.hash,
            "char_id": self.char_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryFragment":
        return cls(
            sig=d.get("sig", ""),
            kind=d.get("kind", ""),
            text=d.get("text", ""),
            source=d.get("source") or {"vol": 0, "ch": 0},
            refs=list(d.get("refs") or []),
            hash=d.get("hash", ""),
            char_id=d.get("char_id"),
        )


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _sig(kind: str, source: dict, text: str) -> str:
    """碎片稳定标识：同 kind + 同位置 + 同内容 → 同一 sig（用于去重与增量更新）。"""
    vol = int(source.get("vol", 0) or 0)
    ch = int(source.get("ch", 0) or 0)
    return f"{kind}:{vol}.{ch}:{_sha1(text)[:10]}"


def _read_json(path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def _src(at: Any) -> dict:
    at = at if isinstance(at, dict) else {}
    return {"vol": int(at.get("vol", 0) or 0), "ch": int(at.get("ch", 0) or 0)}


# ---------------------------------------------------------------- 碎片收割


def harvest_fragments(ws, project_id: str) -> list[MemoryFragment]:
    """从 memory/ 事实源全量收割碎片（剧情事件 / 人物经历 / 关系变化）。

    文件才是持久事实源（ADR-016）；本函数产出的索引随时可删可重建。
    """
    frs: list[MemoryFragment] = []

    events = _read_json(ws._abs(f"{project_id}/memory/plot_events.json"))
    if isinstance(events, list):
        for e in events:
            if not isinstance(e, dict):
                continue
            text = str(e.get("summary") or "")
            if not text:
                continue
            source = _src(e.get("at"))
            # 事件类型进 source（分级检索：大事高权重、日常低权重，讨论决策）
            source = {**source, "type": str(e.get("type") or "")}
            frs.append(
                MemoryFragment(
                    sig=_sig("plot_event", source, text),
                    kind="plot_event",
                    text=text,
                    source=source,
                    refs=[*(e.get("participants") or []), *(e.get("affected_threads") or [])],
                    hash=_sha1(text),
                )
            )

    hist_dir = ws.memory_dir(project_id) / "character_histories"
    if hist_dir.is_dir():
        for f in sorted(hist_dir.glob("*.json")):
            data = _read_json(f)
            if not isinstance(data, dict):
                continue
            char_id = data.get("char_id") or f.stem
            for entry in data.get("entries") or []:
                if not isinstance(entry, dict):
                    continue
                text = str(entry.get("summary") or "")
                if not text:
                    continue
                source = _src(entry.get("at"))
                frs.append(
                    MemoryFragment(
                        sig=_sig("experience", source, f"{char_id}|{text}"),
                        kind="experience",
                        text=text,
                        source=source,
                        refs=[*(entry.get("refs") or []), char_id],
                        hash=_sha1(text),
                        char_id=char_id,
                    )
                )

    rel = _read_json(ws.relationships_path(project_id))
    if isinstance(rel, dict):
        for pair in rel.get("pairs") or []:
            if not isinstance(pair, dict):
                continue
            a, b = pair.get("a"), pair.get("b")
            for entry in pair.get("entries") or []:
                if not isinstance(entry, dict):
                    continue
                text = f"{a} 与 {b}：{entry.get('from')} → {entry.get('to')}"
                source = _src(entry.get("at"))
                frs.append(
                    MemoryFragment(
                        sig=_sig("relationship", source, text),
                        kind="relationship",
                        text=text,
                        source=source,
                        refs=[a, b],
                        hash=_sha1(text),
                    )
                )
    return frs


def query_recent_actual_events(ws, project_id: str, *, limit: int = 8) -> list[dict]:
    """C2/ADR-023 D1 补充：策划低频决策点的**实然事件**确定性查询接口。

    按时间序（故事时间 t → 卷章 → 落盘序）返回最近 `limit` 条已落定的
    `memory/plot_events.json` 真实事件，附带参与者名（bible 名册回查）。
    只读不写；无事件返回 []。

    用途：细纲/事件规划（forge chapter/roll）的前情不再只靠"前一章计划态 gist"，
    正文写偏后仍能查到实际发生的事件（ADR-016：文件即事实源，此处直接读文件，
    不经可再生 RAG 索引）。正文高频注入仍走紧凑前情，不做全量查询。
    """
    path = ws._abs(f"{project_id}/memory/plot_events.json")  # noqa: SLF001
    events = _read_json(path)
    if not isinstance(events, list):
        return []
    chars: dict[str, str] = {}
    cp = ws._abs(f"{project_id}/bible/characters.json")  # noqa: SLF001
    data = _read_json(cp)
    if isinstance(data, list):
        for c in data:
            if isinstance(c, dict) and c.get("id"):
                chars[str(c["id"])] = str(c.get("name") or "?")
    resolved: list[dict] = []
    for i, e in enumerate(events):
        if not isinstance(e, dict):
            continue
        at = e.get("at") if isinstance(e.get("at"), dict) else {}
        try:
            t = int(at.get("t") or 0)
        except (TypeError, ValueError):
            t = 0
        try:
            vol, ch = int(at.get("vol") or 0), int(at.get("ch") or 0)
        except (TypeError, ValueError):
            continue
        summary = str(e.get("summary") or "").strip()
        if not summary:
            continue
        resolved.append({
            "vol": vol, "ch": ch, "t": t, "ord": i,
            "summary": summary,
            "kind": str(e.get("type") or "event"),
            "participants": [chars.get(p, p) for p in (e.get("participants") or [])
                             if isinstance(p, str)],
        })
    resolved.sort(key=lambda x: (x["t"], x["vol"], x["ch"], x["ord"]))
    return resolved[-limit:]


# ---------------------------------------------------------------- 索引


@dataclass
class MemoryIndex:
    """记忆碎片索引（fragment_index.json + rag/vectors.json）。

    向量是**可再生缓存**：删掉 rag/ 后检索自动回落到即时计算，正确性不受影响（ADR-016）。
    """

    fragments: list[MemoryFragment] = field(default_factory=list)
    vectors: dict[str, list[float]] = field(default_factory=dict)
    dim: int | None = None
    kind: str = KEYWORD_KIND
    revision: int = 0

    # ---- 落盘 / 装载 ----
    def save(self, ws, project_id: str) -> None:
        ws.write_json(
            ws.fragment_index_path(project_id),
            {
                "revision": self.revision,
                "kind": self.kind,
                "dim": self.dim,
                "fragments": [f.to_dict() for f in self.fragments],
            },
        )
        rag = ws.rag_dir(project_id)
        if self.vectors:
            ws.write_json(
                rag / "vectors.json",
                {"kind": self.kind, "dim": self.dim, "vectors": self.vectors},
            )

    @classmethod
    def load(cls, ws, project_id: str) -> "MemoryIndex":
        raw = _read_json(ws.fragment_index_path(project_id))
        idx = cls()
        if isinstance(raw, dict):
            idx.revision = int(raw.get("revision", 0) or 0)
            idx.kind = raw.get("kind") or KEYWORD_KIND
            idx.dim = raw.get("dim")
            idx.fragments = [
                MemoryFragment.from_dict(d) for d in (raw.get("fragments") or []) if isinstance(d, dict)
            ]
        vec_raw = _read_json(ws.rag_dir(project_id) / "vectors.json")
        if isinstance(vec_raw, dict):
            idx.vectors = {k: v for k, v in (vec_raw.get("vectors") or {}).items()}
            idx.dim = vec_raw.get("dim", idx.dim)
            idx.kind = vec_raw.get("kind", idx.kind)
        return idx

    # ---- 构建 ----
    def rebuild(self, ws, project_id: str, embedding=None) -> int:
        """从文件全量重建（reindex_memory）。返回碎片数。"""
        emb = embedding or KeywordEmbedding()
        self.fragments = harvest_fragments(ws, project_id)
        self.kind = getattr(emb, "kind", KEYWORD_KIND)
        self.dim = getattr(emb, "dim", None)
        self.vectors = {}
        # 关键词模式不落向量：打分走精确 token（见 MemoryRetriever），向量只是无谓的空间开销
        if self.fragments and self.kind != KEYWORD_KIND:
            vecs = emb.embed([f.text for f in self.fragments])
            self.vectors = {f.sig: v for f, v in zip(self.fragments, vecs)}
        self.revision += 1
        self.save(ws, project_id)
        return len(self.fragments)

    def add(self, frag: MemoryFragment, embedding=None, vector: list[float] | None = None) -> bool:
        """增量加入一条碎片。重复 sig 视为已入库 → 返回 False（不覆盖）。"""
        if any(f.sig == frag.sig for f in self.fragments):
            return False
        if vector is None and embedding is not None and getattr(embedding, "kind", None) != KEYWORD_KIND:
            vector = (embedding.embed([frag.text]) or [[]])[0]
        self.fragments.append(frag)
        if vector:
            self.vectors[frag.sig] = vector
        return True


# ---------------------------------------------------------------- 检索


class MemoryRetriever:
    """检索接口（docs/07 §7.1）。

    语义检索经 EmbeddingProvider；无 embedding 时退化为 `KeywordEmbedding`
    （docs/07 §8 降级总表 / F9.4）。两条路径共用余弦打分，调用方无感。

    分级检索（讨论决策）：plot_event 碎片按事件类型加权——转折/揭秘等大事
    权重更高、日常对话更低，让"先忆"天然偏重关键情节而非流水账。
    """

    # 事件类型 -> 检索权重（大事高、日常低）
    _TYPE_WEIGHT = {
        "turning_point": 1.30,
        "reveal": 1.30,
        "departure": 1.15,
        "conflict": 1.10,
        "discovery": 1.00,
        "dialogue": 0.80,
    }

    def __init__(self, index: MemoryIndex, embedding=None, min_score: float = 0.0) -> None:
        self.index = index
        self.embedding = embedding or KeywordEmbedding()
        self.min_score = min_score
        self._idf: dict[str, float] = {}
        self._idf_n = -1

    # ---- 关键词路径：精确 token 打分 ----
    @property
    def _keyword_mode(self) -> bool:
        return getattr(self.embedding, "kind", KEYWORD_KIND) == KEYWORD_KIND

    def _ensure_idf(self) -> None:
        """按当前语料（索引内的全部碎片）计算 IDF，压低"苏晚""掌门"这类高频词的权重。"""
        n = len(self.index.fragments)
        if n == self._idf_n:
            return
        df: dict[str, int] = {}
        for f in self.index.fragments:
            for t in set(tokenize(f.text)):
                df[t] = df.get(t, 0) + 1
        self._idf = {t: math.log(1 + n / (1 + c)) for t, c in df.items()}
        self._idf_n = n

    def _keyword_score(self, q_tokens: list[str], doc_tokens: list[str]) -> float:
        """IDF 加权 token 集合余弦。

        不用哈希向量做余弦——定长哈希在小语料上碰撞噪声会盖过真实信号
        （实测：dim=256 时"零重合"文档能拿到 0.16 分，高于真实命中的 0.12）。
        精确集合运算没有碰撞，重合为 0 就是 0。
        """
        self._ensure_idf()
        qt, dt = set(q_tokens), set(doc_tokens)
        shared = qt & dt
        if not shared:
            return 0.0
        w = lambda t: self._idf.get(t, math.log(1 + max(1, self._idf_n)))  # noqa: E731
        num = sum(w(t) for t in shared)
        nq = math.sqrt(sum(w(t) ** 2 for t in qt)) or 1.0
        nd = math.sqrt(sum(w(t) ** 2 for t in dt)) or 1.0
        return num / (nq * nd)

    # ---- 语义路径：向量余弦 ----
    def _vector_for(self, frag: MemoryFragment) -> list[float]:
        v = self.index.vectors.get(frag.sig)
        if v:
            return v
        # 索引里没有向量（rag/ 被删或增量写入时未提供 embedding）→ 即时计算并回填
        v = (self.embedding.embed([frag.text]) or [[]])[0]
        if v:
            self.index.vectors[frag.sig] = v
        return v

    def _match(self, frag: MemoryFragment, f: dict) -> bool:
        if not f:
            return True
        if "char_id" in f and f["char_id"]:
            cid = f["char_id"]
            if frag.char_id != cid and cid not in frag.refs:
                return False
        kinds = f.get("kinds")
        if kinds and frag.kind not in kinds:
            return False
        scope = f.get("chapter_scope")
        if isinstance(scope, dict) and scope:
            if "vol" in scope and int(frag.source.get("vol", 0)) != int(scope["vol"]):
                return False
            if "ch" in scope and int(frag.source.get("ch", 0)) != int(scope["ch"]):
                return False
        after = f.get("after")
        if isinstance(after, dict) and after and not _ge(frag.source, after):
            return False
        before = f.get("before")
        if isinstance(before, dict) and before and not _le(frag.source, before):
            return False
        return True

    def query(self, q: MemoryQuery) -> list[MemoryHit]:
        filters = q.filters or {}
        cand = [f for f in self.index.fragments if self._match(f, filters)]
        if not cand:
            return []
        if self._keyword_mode:
            q_tokens = tokenize(q.query)
            scores = {f.sig: self._keyword_score(q_tokens, tokenize(f.text)) for f in cand}
        else:
            qv = (self.embedding.embed([q.query]) or [[]])[0]
            scores = {f.sig: cosine(qv, self._vector_for(f)) for f in cand}
        scored = [
            MemoryHit(
                sig=f.sig,
                kind=f.kind,
                text=f.text,
                source=dict(f.source),
                refs=list(f.refs),
                score=round(scores[f.sig] * self._TYPE_WEIGHT.get(f.source.get("type", ""), 1.0), 6),
            )
            for f in cand
        ]
        floor = max(q.min_score, self.min_score)
        scored = [h for h in scored if h.score >= floor]
        scored.sort(key=lambda h: (-h.score, h.source.get("vol", 0), h.source.get("ch", 0), h.sig))
        return scored[: q.top_k]


def _ge(a: dict, b: dict) -> bool:
    return (int(a.get("vol", 0)), int(a.get("ch", 0))) >= (int(b.get("vol", 0)), int(b.get("ch", 0)))


def _le(a: dict, b: dict) -> bool:
    return (int(a.get("vol", 0)), int(a.get("ch", 0))) <= (int(b.get("vol", 0)), int(b.get("ch", 0)))


# ---------------------------------------------------------------- 写入


@dataclass
class MemoryWriter:
    """写入接口（sensitive，编纂员/主编剧）（docs/07 §7.2）。

    每条写入前做**冲突双检**：
    - 规则层（本模块）：重复入库（同 sig 已在索引中）、bible 引用完整性（`char:`/`pt:`/`loc:`）。
    - 语义层（可选）：`semantic_checker(new_text, existing_texts)` 返回 False 即判冲突。
      未注入时跳过——语义检由编纂员子代理承担，编排层负责注入（docs/05 §5.4 第 4 步）。

    写入后增量更新索引（docs/06 §4.4 `indexed`），使下一事件/下一章立即可"先忆"（F11.5）。
    """

    ws: Any
    project_id: str
    index: MemoryIndex | None = None
    embedding: Any = None
    semantic_checker: Callable[[str, list[str]], bool | None] | None = None

    def _ensure_index(self) -> MemoryIndex:
        if self.embedding is None:
            self.embedding = KeywordEmbedding()
        if self.index is None:
            p = self.ws.fragment_index_path(self.project_id)
            if p.exists():
                self.index = MemoryIndex.load(self.ws, self.project_id)
            else:
                idx = MemoryIndex()
                idx.rebuild(self.ws, self.project_id, self.embedding)
                self.index = idx
        return self.index

    # ---- 冲突双检 ----
    def _check_duplicate(self, kind: str, source: dict, text: str) -> None:
        idx = self._ensure_index()
        sig = _sig(kind, source, text)
        if any(f.sig == sig for f in idx.fragments):
            raise MemoryConflictError(
                f"记忆重复：{kind} @ {source.get('vol')}:{source.get('ch')} 的相同内容已在记忆层（sig={sig}）"
            )

    def _check_refs(self, refs: Iterable[str]) -> None:
        """bible 引用完整性：前缀已知的 id 必须在对应 bible 文件中建档（docs/06 §5.2）。"""
        for ref in refs or []:
            if not isinstance(ref, str) or ":" not in ref:
                continue
            prefix = ref.split(":", 1)[0] + ":"
            rel = _BIBLE_SOURCES.get(prefix)
            if not rel:
                continue
            data = _read_json(self.ws._abs(f"{self.project_id}/{rel}"))
            if data is None:
                continue  # bible 文件不存在 → 无从校验，放行（初始化期允许）
            known = {d.get("id") for d in data if isinstance(d, dict)} if isinstance(data, list) else set()
            if known and ref not in known:
                raise MemoryConflictError(f"记忆引用了未建档的实体 {ref}（{rel} 中不存在，需先建档或人工仲裁）")

    def _check_semantic(self, text: str) -> None:
        if self.semantic_checker is None:
            return
        idx = self._ensure_index()
        verdict = self.semantic_checker(text, [f.text for f in idx.fragments])
        if verdict is False:
            raise MemoryConflictError(f"语义冲突：新增记忆与既有记忆矛盾，需人工仲裁：{text[:60]}")

    # ---- 写入 ----
    def append_plot_event(self, event: dict) -> dict:
        """追加一条剧情事件到 memory/plot_events.json（docs/06 §3.5）。"""
        summary = str(event.get("summary") or "")
        if not summary:
            raise MemoryConflictError("plot_event 缺少 summary")
        source = _src(event.get("at"))
        self._check_duplicate("plot_event", source, summary)
        self._check_refs([*(event.get("participants") or []), *(event.get("affected_threads") or [])])
        self._check_semantic(summary)

        path = self.ws._abs(f"{self.project_id}/memory/plot_events.json")
        events = _read_json(path)
        events = events if isinstance(events, list) else []
        events.append(event)
        self.ws.write_json(path, events)

        idx = self._ensure_index()
        frag = MemoryFragment(
            sig=_sig("plot_event", source, summary),
            kind="plot_event",
            text=summary,
            source=source,
            refs=[*(event.get("participants") or []), *(event.get("affected_threads") or [])],
            hash=_sha1(summary),
        )
        idx.add(frag, embedding=self.embedding)
        idx.revision += 1
        idx.save(self.ws, self.project_id)
        return event

    def append_experience(self, char_id: str, entry: dict) -> dict:
        """为角色追加一条人物经历（docs/06 §3.5 character_histories）。"""
        summary = str(entry.get("summary") or "")
        if not summary:
            raise MemoryConflictError("experience 缺少 summary")
        source = _src(entry.get("at"))
        self._check_duplicate("experience", source, f"{char_id}|{summary}")
        self._check_refs([*(entry.get("refs") or []), char_id])
        self._check_semantic(summary)

        path = self.ws.char_history_path(self.project_id, char_id)
        data = _read_json(path)
        data = data if isinstance(data, dict) else {}
        entries = list(data.get("entries") or [])
        entries.append(entry)
        payload = {"char_id": char_id, "revision": int(data.get("revision", 0) or 0) + 1, "entries": entries}
        self.ws.write_json(path, payload)

        idx = self._ensure_index()
        frag = MemoryFragment(
            sig=_sig("experience", source, f"{char_id}|{summary}"),
            kind="experience",
            text=summary,
            source=source,
            refs=[*(entry.get("refs") or []), char_id],
            hash=_sha1(summary),
            char_id=char_id,
        )
        idx.add(frag, embedding=self.embedding)
        idx.revision += 1
        idx.save(self.ws, self.project_id)
        return payload

    def record_relationship_change(self, a: str, b: str, entry: dict) -> dict:
        """记录一次关系变化到 memory/relationships.json（docs/06 §3.5）。"""
        text = f"{a} 与 {b}：{entry.get('from')} → {entry.get('to')}"
        source = _src(entry.get("at"))
        self._check_duplicate("relationship", source, text)
        self._check_refs([a, b])

        path = self.ws.relationships_path(self.project_id)
        data = _read_json(path)
        data = data if isinstance(data, dict) else {}
        pairs = list(data.get("pairs") or [])
        for pair in pairs:
            if isinstance(pair, dict) and {pair.get("a"), pair.get("b")} == {a, b}:
                pair.setdefault("entries", []).append(entry)
                break
        else:
            pairs.append({"a": a, "b": b, "entries": [entry]})
        payload = {"pairs": pairs}
        self.ws.write_json(path, payload)

        idx = self._ensure_index()
        idx.add(
            MemoryFragment(
                sig=_sig("relationship", source, text),
                kind="relationship",
                text=text,
                source=source,
                refs=[a, b],
                hash=_sha1(text),
            ),
            embedding=self.embedding,
        )
        idx.revision += 1
        idx.save(self.ws, self.project_id)
        return payload


    # ---- 修订与回退（B-07）----
    def drop_synthetic_chapter(self, vol: int, ch: int) -> int:
        """删除某一章的章级合成事件（type=chapter）。

        与 `drop_by_source` 的"合成事件保留"不矛盾：那边是情节回滚（真实记忆撤
        回、进度记录不动）；这里是**提交前消歧**——合成事件与真实事件互斥、且自身
        幂等，新事件落库前同章旧合成记录一律清除（由 `commit_event` 调用）。
        返回删除条数。
        """
        idx = self._ensure_index()
        path = self.ws._abs(f"{self.project_id}/memory/plot_events.json")  # noqa: SLF001
        events = _read_json(path)
        if not isinstance(events, list):
            return 0
        kept, removed = [], 0
        for e in events:
            if (isinstance(e, dict) and e.get("type") == "chapter"
                    and isinstance(e.get("at"), dict)
                    and int(e["at"].get("vol", -1)) == vol and int(e["at"].get("ch", -1)) == ch):
                removed += 1
                continue
            kept.append(e)
        if removed:
            self.ws.write_json(path, kept)  # noqa: SLF001
            idx.rebuild(self.ws, self.project_id, self.embedding)
        return removed

    def drop_by_source(self, vol: int, ch: int, *, kinds: Iterable[str] | None = None) -> dict:
        """回退某一章写入的真实记忆（章节重写 / 人工撤销时用）。

        docs/06 §4.4 声称冲突记忆"可回滚"，但原先只有 append_*，撤回能力缺失——
        导致章节重生成后新旧事件并存、重复且矛盾。本方法补上这条回退路径。

        `kinds` 默认含全部真实类型；`chapter` 型合成事件始终保留（它是系统的
        章级进度记录，不属于情节记忆）。
        """
        idx = self._ensure_index()
        drop_kinds = set(kinds) if kinds else {"plot_event", "experience", "relationship"}
        removed = {"plot_events": 0, "experiences": 0, "relationships": 0}

        if "plot_event" in drop_kinds:
            path = self.ws._abs(f"{self.project_id}/memory/plot_events.json")
            events = _read_json(path)
            if isinstance(events, list):
                kept = []
                for e in events:
                    if not isinstance(e, dict):
                        continue
                    at = e.get("at")
                    is_synthetic = e.get("type") == "chapter"
                    if (not is_synthetic and isinstance(at, dict)
                            and int(at.get("vol", -1)) == vol and int(at.get("ch", -1)) == ch):
                        removed["plot_events"] += 1
                        continue
                    kept.append(e)
                self.ws.write_json(path, kept)

        if "experience" in drop_kinds:
            hist_dir = self.ws.memory_dir(self.project_id) / "character_histories"
            if hist_dir.is_dir():
                for f in sorted(hist_dir.glob("*.json")):
                    data = _read_json(f)
                    if not isinstance(data, dict):
                        continue
                    old = list(data.get("entries") or [])
                    entries = [x for x in old
                               if not (isinstance(x, dict) and (x.get("at") or {}).get("vol") == vol
                                       and (x.get("at") or {}).get("ch") == ch)]
                    if len(entries) != len(old):
                        removed["experiences"] += len(old) - len(entries)
                        data["entries"] = entries
                        data["revision"] = int(data.get("revision", 0) or 0) + 1
                        self.ws.write_json(f, data)

        if "relationship" in drop_kinds:
            path = self.ws.relationships_path(self.project_id)
            data = _read_json(path)
            if isinstance(data, dict):
                pairs = []
                for pair in data.get("pairs") or []:
                    if not isinstance(pair, dict):
                        continue
                    old = list(pair.get("entries") or [])
                    kept = [x for x in old
                            if not (isinstance(x, dict) and (x.get("at") or {}).get("vol") == vol
                                    and (x.get("at") or {}).get("ch") == ch)]
                    removed["relationships"] += len(old) - len(kept)
                    if kept:
                        pair["entries"] = kept
                        pairs.append(pair)
                self.ws.write_json(path, {"pairs": pairs})

        idx.rebuild(self.ws, self.project_id, self.embedding)
        return removed

    def revise_fragment(self, sig: str, new_text: str) -> bool:
        """修订索引中一条记忆碎片的文本（人工仲裁后的更正路径）。

        只改索引文本与向量；`memory/` 事实源里的原始条目不动——事实源的更正应
        由编纂员以 append 追加「更正条目」的方式完成，保留审计线索（docs/09 §5）。
        """
        idx = self._ensure_index()
        for f in idx.fragments:
            if f.sig == sig:
                f.text = new_text
                f.hash = _sha1(new_text)
                if self.embedding is not None and getattr(self.embedding, "kind", None) != KEYWORD_KIND:
                    idx.vectors[sig] = (self.embedding.embed([new_text]) or [[]])[0]
                idx.revision += 1
                idx.save(self.ws, self.project_id)
                return True
        return False


def rollback_chapter(ws, project_id: str, vol: int, ch: int, embedding=None) -> dict:
    """回退某一章写入的真实记忆（章节重生成前的清理）。返回删除计数。"""
    return MemoryWriter(ws, project_id, embedding=embedding).drop_by_source(vol, ch)


def reindex_memory(ws, project_id: str, embedding=None) -> int:
    """全量重建记忆索引（docs/07 §7.3）。返回碎片数。文件是事实源，索引可随时重建。"""
    idx = MemoryIndex()
    return idx.rebuild(ws, project_id, embedding=embedding)
