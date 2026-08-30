"""知识检索层（RAG 增强，讨论第 8 轮：prompt 注入的检索化 + 向量化 + LLM 查询生成）。

## 问题

system_prompt 里 5 块"应然静态全量"（出场人物全卡 ≤16 人、世界设定前 6 势力、
历史教训前 5 条、伏笔前 5 条）在长卷（20 章）下随内容线性膨胀，且大量与当前事件
无关——注入即注意力稀释。

## 方案（用户拍板：三件都做）

把可检索知识统一成 `KnowledgeItem`（设定/人物/伏笔/教训/势力/物品/功法），三层检索：

1. **确定性**：关键词子串命中（便宜、兜底，settings 的 keywords 表）；
2. **语义**：向量化后余弦（nomic-embed，召回同义表述）；无 embedding 自动退化关键词；
3. **LLM 查询生成**：`plan_queries` 让模型决定"本事件需要哪些知识"，用生成的查询检索
   ——不再只靠关键词蒙，而是 AI 主动提出知识需求。

融合结果按 kind 分组截断，交事件级 prompt 注入（相关设定/人物卡/伏笔/教训段）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .embedding import KEYWORD_KIND, KeywordEmbedding, cosine
from .llm import LLMMessage, LLMRequest

QUERY_PROMPT = """你是检索规划器。动笔写下面这个剧情事件之前，先想清楚需要查阅哪些知识：
世界设定（境界/规则/组织）、人物背景、未回收伏笔、历史教训、物品功法。
事件：【{ev_text}】
输出 3-5 个查询词（逗号分隔，每个不超过 12 字），直接输出查询词本身，不要解释。
"""

# 每 kind 注入上限（防事件 prompt 膨胀）
KIND_TOPK = {
    "setting": 3, "character": 4, "thread": 2, "lesson": 2,
    "faction": 2, "item": 2, "skill": 2,
}


@dataclass
class KnowledgeItem:
    kind: str          # setting | character | thread | lesson | faction | item | skill
    id: str
    text: str          # 检索文本（关键词 + 说明，供语义/关键词匹配）
    keywords: list[str] = field(default_factory=list)
    payload: dict = field(default_factory=dict)  # 注入形态（卡片行/设定文本…）


class KnowledgeBase:
    """统一知识库：收集 bible 各类条目 → 向量化 → 融合检索。"""

    def __init__(self, ws, project_id: str, embedding=None) -> None:
        self.embedding = embedding or KeywordEmbedding()
        self._items: list[KnowledgeItem] = []
        self._vectors: dict[str, list[float]] = {}
        self._build(ws, project_id)
        self._ensure_vectors()

    # ---------------------------------------------------------------- 构建
    def _build(self, ws, project_id: str) -> None:
        def read(rel: str):
            p = ws._abs(f"{project_id}/{rel}")
            if not p.exists():
                return []
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                return []

        # 设定条目（settings.json，M3g）
        for s in read("bible/settings.json"):
            if isinstance(s, dict) and s.get("id"):
                kw = [str(k) for k in (s.get("keywords") or [])]
                self._items.append(KnowledgeItem(
                    kind="setting", id=str(s["id"]), text=f"{' '.join(kw)} {s.get('text', '')}",
                    keywords=kw, payload={"text": str(s.get("text") or "")}))

        # 世界状态（实然）：当前修为/位置/物品，随人物卡事件级注入（战力硬约束）
        state_lines: dict[str, str] = {}
        ws_data = read("bible/worldstate.json")
        if isinstance(ws_data, dict):
            for cid, cur in (ws_data.get("characters") or {}).items():
                if not isinstance(cur, dict):
                    continue
                bits = [f"{cur.get('name') or cid}"]
                if cur.get("realm"):
                    bits.append(f"当前修为 {cur['realm']}")
                if cur.get("location"):
                    bits.append(f"所在地 {cur['location']}")
                if cur.get("items"):
                    bits.append("持有 " + "、".join(cur["items"]))
                if cur.get("injuries"):
                    bits.append("伤势 " + "、".join(cur["injuries"]))
                if cur.get("dead"):
                    bits.append("已死亡（不得再出场行动）")
                if len(bits) > 1:
                    state_lines[cid] = "，".join(bits[1:])

        # 人物卡（characters.json）
        for c in read("bible/characters.json"):
            if not isinstance(c, dict) or not c.get("id"):
                continue
            pw = c.get("power") or {}
            names = [c.get("name", "")] + [str(a) for a in (c.get("aliases") or [])]
            traits = c.get("core_traits") or []
            text = " ".join(names) + " " + " ".join(traits) + " " + str(pw.get("level") or "")
            self._items.append(KnowledgeItem(
                kind="character", id=str(c["id"]), text=text,
                keywords=[n for n in names if n],
                payload={"card": c, "state": state_lines.get(str(c["id"]), "")}))

        # 伏笔（plot_threads.json）
        for t in read("bible/plot_threads.json"):
            if isinstance(t, dict) and t.get("id") and t.get("status") in ("planted", "active"):
                self._items.append(KnowledgeItem(
                    kind="thread", id=str(t["id"]),
                    text=f"{t.get('id')} {t.get('desc', '')}",
                    keywords=[str(t.get("id") or "")], payload={"desc": str(t.get("desc") or "")}))

        # 历史教训（review_lessons.json，M3h）
        for l in read("bible/review_lessons.json"):
            if isinstance(l, dict) and l.get("rule"):
                self._items.append(KnowledgeItem(
                    kind="lesson", id=str(l.get("_key") or ""),
                    text=f"{l.get('category', '')} {l.get('rule', '')}",
                    keywords=[str(l.get("category") or "")],
                    payload={"rule": str(l.get("rule") or "")[:120]}))

        # 势力（worldview.factions）
        wv = read("bible/worldview.json")
        if isinstance(wv, dict):
            for f in wv.get("factions") or []:
                if isinstance(f, dict) and f.get("faction"):
                    self._items.append(KnowledgeItem(
                        kind="faction", id=str(f["faction"]),
                        text=f"{f.get('faction')} {f.get('note', '')}",
                        keywords=[str(f.get("faction") or "")],
                        payload={"note": str(f.get("note") or "")}))

        # 物品/功法注册表
        for rel, kind in (("bible/items.json", "item"), ("bible/skills.json", "skill")):
            for e in read(rel):
                if isinstance(e, dict) and e.get("name"):
                    self._items.append(KnowledgeItem(
                        kind=kind, id=str(e.get("id") or ""),
                        text=" ".join([str(e.get("name") or "")] + [str(a) for a in (e.get("aliases") or [])]
                                     + [str(e.get("note") or "")]),
                        keywords=[str(e.get("name") or "")],
                        payload={"note": str(e.get("note") or "")}))

    # ---------------------------------------------------------------- 向量化
    def _ensure_vectors(self) -> None:
        if getattr(self.embedding, "kind", KEYWORD_KIND) == KEYWORD_KIND:
            return  # 关键词模式不向量化
        try:
            vecs = self.embedding.embed([it.text for it in self._items])
            self._vectors = {it.id: v for it, v in zip(self._items, vecs) if v}
        except Exception:  # noqa: BLE001 - 向量化失败退化关键词
            self._vectors = {}

    def _vec(self, item: KnowledgeItem) -> list[float]:
        if self._vectors.get(item.id):
            return self._vectors[item.id]
        if self._vectors:
            try:
                v = (self.embedding.embed([item.text]) or [[]])[0]
                if v:
                    self._vectors[item.id] = v
                return v
            except Exception:  # noqa: BLE001
                return []
        return []

    # ---------------------------------------------------------------- 检索
    def keyword_hits(self, text: str) -> list[KnowledgeItem]:
        """确定性：关键词子串命中 或 token 交集（与 MemoryRetriever 同口径）。

        token 交集让「越级」这类语义联想在 keyword 模式下也有兜底召回
        （lesson「战力越级…」与查询「修为越级碾压」共享二元组）。
        """
        if not text:
            return []
        from .memory import tokenize

        q_tokens = set(tokenize(text))
        out = []
        for it in self._items:
            hit = False
            for kw in it.keywords:
                if kw and kw in text:
                    hit = True
                    break
            if not hit and q_tokens and q_tokens & set(tokenize(it.text)):
                hit = True
            if hit:
                out.append(it)
        return out

    def semantic_topk(self, text: str, top_k: int = 6) -> list[KnowledgeItem]:
        """语义：查询向量与条目向量余弦。关键词模式退化为 keyword_hits。"""
        if getattr(self.embedding, "kind", KEYWORD_KIND) == KEYWORD_KIND:
            return self.keyword_hits(text)[:top_k]
        try:
            qv = (self.embedding.embed([text]) or [[]])[0]
        except Exception:  # noqa: BLE001
            return self.keyword_hits(text)[:top_k]
        if not qv:
            return self.keyword_hits(text)[:top_k]
        scored = sorted(
            ((it, cosine(qv, self._vec(it))) for it in self._items if self._vec(it)),
            key=lambda x: x[1], reverse=True)
        return [it for it, s in scored if s > 0.0][:top_k]

    def retrieve(self, query: str, top_k: int = 6) -> list[KnowledgeItem]:
        """融合检索：语义 top-k ∪ 关键词命中，按 kind 分组截断。"""
        merged: dict[str, KnowledgeItem] = {}
        for it in self.semantic_topk(query, top_k * 3):
            merged.setdefault(it.id, it)
        for it in self.keyword_hits(query):
            merged.setdefault(it.id, it)
        grouped: dict[str, list[KnowledgeItem]] = {}
        for it in merged.values():
            grouped.setdefault(it.kind, []).append(it)
        out: list[KnowledgeItem] = []
        for kind, its in grouped.items():
            out.extend(its[:KIND_TOPK.get(kind, 2)])
        return out

    def plan_queries(self, ev_text: str, provider=None, max_q: int = 4) -> list[str]:
        """LLM 查询生成：让模型决定本事件需要哪些知识（讨论第 8 轮·用户设想）。

        无 provider / 失败时回退为事件文本本身（确定性+语义照常工作）。
        """
        if provider is None or not ev_text.strip():
            return [ev_text]
        try:
            res = provider.complete(LLMRequest(
                messages=[LLMMessage(role="user",
                                     content=QUERY_PROMPT.format(ev_text=ev_text[:200]))],
                max_tokens_out=120, temperature=0.2))
        except Exception:  # noqa: BLE001
            return [ev_text]
        if not res.content or res.blocked:
            return [ev_text]
        qs = [q.strip().strip("，。、") for q in res.content.replace("\n", ",").split(",")]
        qs = [q for q in qs if q][:max_q]
        return qs or [ev_text]

    # ---------------------------------------------------------------- 注入形态
    def lines(self, items: list[KnowledgeItem], kind: str, *, vol: int | None = None,
              ch: int | None = None) -> list[str]:
        """把某 kind 的命中条目转成 prompt 行（供事件级注入）。

        character 行附带：当前状态（worldstate 实然，战力硬约束）与首现标记
        （讨论第 6 轮：首次出场通过行动自然带出，不写成简介）。
        """
        out = []
        for it in items:
            if it.kind != kind:
                continue
            if kind == "setting":
                out.append(f"- {it.payload.get('text', '')}")
            elif kind == "character":
                c = it.payload.get("card") or {}
                pw = c.get("power") or {}
                line = (f"- {c.get('name')}（{pw.get('level') or '?'}，"
                        f"{pw.get('faction') or ''}）：性格{'、'.join(c.get('core_traits') or []) or '—'}"
                        f"{'；弧线：' + str(c.get('arc')) if c.get('arc') else ''}")
                fa = c.get("first_appear") or {}
                if vol and ch and int(fa.get("vol", 0) or 0) == vol and int(fa.get("ch", 0) or 0) == ch:
                    line += "（本章首次出场：让读者通过行动/对白自然认识他，不要写成简介）"
                if it.payload.get("state"):
                    line += f"；当前状态：{it.payload['state']}"
                out.append(line)
            elif kind == "thread":
                out.append(f"- {it.payload.get('desc', '')}")
            elif kind == "lesson":
                out.append(f"- {it.payload.get('rule', '')}")
            elif kind == "faction":
                out.append(f"- {it.id}：{it.payload.get('note', '')}")
            elif kind in ("item", "skill"):
                out.append(f"- {it.id}：{it.payload.get('note', '')}")
        return out
