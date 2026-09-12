"""文本/数据归一化（确定性、零 LLM）。

**单一源**：装配侧（`core/context.py` 渲染 prompt）与 Forge 写入侧（`forge/nodes.py`
落 bible）共用同一套清洗，避免两处各写一份然后慢慢漂移。

## 为什么需要

forge 的 LLM 产出会带三类"同一信息重复渲染"的脏数据——都属实测（2026-09-12 prompt 审计
P0-3 / P0-4，`docs/prompt审计-2026-09-12.md`），既浪费 prompt 预算，又把详细条目挤到后面：

1. **禁令表精确重复**：实测 38 条里 `毫无疑问地` / `毫无疑问的` 各出现 2 次。
2. **glossary 的 `term` 被写成顿号清单**：16 个真实项目里 4 个命中（4/56 条），
   如 `'【出名就变强系统】、声望值、明善暗恶'`——渲染出来是一串空括号词组，
   且把后面本该生效的详细条目顶在后面。
3. **同一 term 既有骨架条目（note 空）又有详细条目**：渲染两次，模型先读到信息量最低的那个。

数据侧不做"同义/近义"归并（例如把「毫无疑问」族合成一条）——那需要语义判断，
属批次 3 的生成侧治理，不能藏在确定性清洗里假装做到了。
"""

from __future__ import annotations

# term 里出现这些标点 → 判定为"描述句"而非"顿号清单"，不拆分。
# 真实反例（都不是清单，拆了就成碎片）：
#   '灵气复苏等级体系（如：觉醒者、超凡者、半仙、真仙等对应低武/高武/修仙阶段）'
#   '无敌领域系统：江浩的金手指，核心机制'
_TERM_LIST_BLOCKERS = ("（", "(", "）", ")", "：", ":", "，", ",", "；", ";", "。", "/")

# glossary 渲染时 note 字段名。schema 早期写作 `def`（见 schemas/bible/style.schema.json），
# 但代码与全部实测数据都用 `note`——此处兼容读两种，写回统一用 `note`。
_NOTE_KEYS = ("note", "def")


def dedup_keep_order(items) -> list[str]:
    """按原序去重（保留首次出现位置），丢弃空白项与非字符串项。"""
    return list(dict.fromkeys(s for s in (str(x).strip() for x in items) if s))


def split_terms(term: str) -> list[str]:
    """把「A、B、C」式 term 拆成原子术语；含其他标点的描述句原样返回（单元素）。"""
    if any(c in term for c in _TERM_LIST_BLOCKERS):
        return [term]
    parts = [p.strip() for p in term.split("、")]
    return [p for p in parts if p] or [term]


def _glossary_note(g: dict) -> str:
    for k in _NOTE_KEYS:
        v = g.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def normalize_glossary(glossary) -> list[dict]:
    """术语表归一 → `[{term, note}, ...]`（保留首见条目的其他键）。

    规则：
    - 拆分顿号清单式 term（`split_terms`）；
    - 同 term 只留一条，note 取**更长**的那个（骨架条目被详细条目覆盖）；
    - 拆分出来的原子术语**不带 note**——原 note 属于整组，归给任一条都是误导；
    - 顺序 = 原子术语首现顺序（dict 保序）。
    """
    out: dict[str, dict] = {}
    for g in glossary:
        if not isinstance(g, dict):
            continue
        raw = str(g.get("term") or "").strip()
        if not raw:
            continue
        note = _glossary_note(g)
        parts = split_terms(raw)
        note_for_parts = note if len(parts) == 1 else ""
        for part in parts:
            if part not in out:
                item = dict(g)
                item["term"] = part
                item["note"] = note_for_parts
                out[part] = item
            elif len(note_for_parts) > len(str(out[part].get("note") or "")):
                out[part]["note"] = note_for_parts
    return list(out.values())
