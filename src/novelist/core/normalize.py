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

第 4 类脏数据（2026-09-12 结构化审计批次 2）是**机器字段键泄漏**：模型把世界观
分类字段吐成 `{"social_structure": "…"}` 或逐字段拍平成 `{"faction": "name：玄剑宗"}`，
`_coerce_str_list` 按 `f"{k}：{v}"` 字符串化后，中文 prompt 里就出现
`social_structure：以修士为核心…`。它不是"信息错"，但**会毁掉下游的身份识别**——
`knowledge.py` 拿 `factions[].faction` 当知识单元 id、`character_factory` 拿它当
`power.faction` 取值表，于是势力检索与 faction 校验同时失效。故此处做两件事：
`humanize_kv`（渲染侧剥离/翻译 ASCII 键）与 `regroup_factions`（结构侧重新分组）。
"""

from __future__ import annotations

import re

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


# ---------------------------------------------------------------- 机器字段键

# 纯 ASCII 标识符键（`social_structure` / `power_level`）。中文 prompt 里它是机器噪声，
# 但**中文键**（`社会结构`）本身有语义（模型自己写的中文分类名），必须原样保留——
# 这是本模块只剥 ASCII 键、不碰中文键的原因。
_ASCII_KV = re.compile(r"^([A-Za-z][A-Za-z0-9_]*)\s*[：:]\s*(.*)$", re.S)

# 实测高频英文键 → 中文标签。取自 16 个真实项目的 worldview
# civilizations / systems / factions 字段（2026-09-12 全量扫描）。
_KV_LABELS: dict[str, str] = {
    "name": "名称", "title": "名称", "desc": "描述", "description": "描述",
    "summary": "概要", "status": "状态", "structure": "结构",
    "social_structure": "社会结构", "cultural_norms": "文化规范",
    "economic_system": "经济体系", "political_system": "政治体系",
    "type": "类型", "kind": "类型", "level": "层级", "levels": "层级",
    "faction": "势力", "alignment": "阵营", "goal": "目标", "goals": "目标",
    "core_strength": "核心实力", "conflict": "冲突", "features": "特征",
    "rule": "规则", "rules": "规则", "mechanic": "机制", "note": "说明",
}

# 判定"这一条是一个新势力的开头"的键名（拍平数据的唯一分组锚点）。
_NAME_KEYS = frozenset({"name", "title", "faction"})


def split_kv(text) -> tuple[str, str] | None:
    """`英文键：值` → `(键, 值)`；无 ASCII 标识符键（或非字符串）返回 None。"""
    if not isinstance(text, str):
        return None
    m = _ASCII_KV.match(text.strip())
    return (m.group(1), m.group(2).strip()) if m else None


def humanize_kv(text) -> str:
    """剥离机器字段键：`social_structure：以修士为核心` → `社会结构：以修士为核心`。

    - 已知英文键 → 中文标签；未知英文键 → **只留值**（中文 prompt 里一个裸英文标识符
      没有信息价值，而它的值仍完整可读）；
    - 中文键与普通句子原样返回（中文键是有语义的，不能当噪声剥掉）。
    """
    if not isinstance(text, str):
        return str(text)
    kv = split_kv(text)
    if not kv:
        return text.strip()
    key, val = kv
    if not val:
        return _KV_LABELS.get(key.lower(), "")
    label = _KV_LABELS.get(key.lower())
    return f"{label}：{val}" if label else val


def regroup_factions(items) -> list[dict]:
    """势力条目归一 → `[{"faction": <名>, "note": <其余字段>, ...}]`。

    识别"逐字段拍平"脏数据并**按 `name` 键重新分组**；不含 ASCII 键的条目走原路
    （行为与 `forge/nodes.py` 原实现逐字一致）。

    实测（2026-09-12）：模型把 3 个势力的 12 个字段吐成 12 条
    `{"faction": "name：玄剑宗"}` / `{"faction": "type：正道宗门"}` / `{"faction": "description：…"}`，
    逐项落盘后 `faction` = `"name：玄剑宗"` → knowledge 势力单元 id 与 character_factory
    的 `power.faction` 取值表同时失效。此处按 name 键切分重组，其余字段合并进 `note`。
    """
    entries: list[tuple[dict, str]] = []
    for it in items or []:
        if isinstance(it, str) and it.strip():
            entries.append(({}, it.strip()))
        elif isinstance(it, dict) and isinstance(it.get("faction"), str) and it["faction"].strip():
            entries.append((it, it["faction"].strip()))
    if not any(split_kv(val) for _, val in entries):
        # 非拍平数据：维持原行为（str → {"faction": s}；dict 原样）
        return [dict(src) if src else {"faction": val} for src, val in entries]

    groups: list[dict] = []
    cur: dict | None = None
    extras: list[str] = []

    def close() -> None:
        nonlocal cur, extras
        if cur and cur.get("faction"):
            if extras:
                cur["note"] = "；".join(x for x in extras if x)
            groups.append(cur)
        cur, extras = None, []

    for src, val in entries:
        kv = split_kv(val)
        key, value = kv if kv else ("", val)
        if key.lower() in _NAME_KEYS:
            close()
            cur = {"faction": value}
            continue
        if cur is None:
            cur = {"faction": ""}
        label = _KV_LABELS.get(key.lower())
        extras.append(f"{label}：{value}" if label and value else value)
    close()
    return [g for g in groups if g.get("faction")]

# ---------------------------------------------------------------------------
# LLM JSON 容错解析（docs/04 §5.7「结构化输出校验失败 → 有限重试 → 宽松解析」）
# ---------------------------------------------------------------------------
# 2026-09-16 真机事故（proj-20260914233722）：thread_set 节点连续两轮 JSON 语法错误
# （`Expecting ',' delimiter: line 183 column 8`）→ 重试盲发同一 prompt（模型看不到
# 上次错在哪）→ 仍失败 → 回退。此处补两件事：① 结构位的全角标点/尾逗号可修复；
# ② 失败时给出**带行号与上下文的诊断**，供重试 prompt 携带。

_FULLWIDTH_STRUCT = {
    "，": ",", "：": ":", "；": ";", "、": ",",
    "（": "(", "）": ")", "【": "[", "】": "]",
    "｛": "{", "｝": "}", "［": "[", "］": "]",
    "＂": '"', "“": '"', "”": '"', "‘": "'", "’": "'",
}


def fix_structural_punctuation(text: str) -> str:
    """把**字符串字面量之外**的全角标点换成半角（字符串内的原文一字不动）。

    模型偶尔在 JSON 结构位写中文标点（`"a": 1，` / `{“k”: v}`），这是个确定性可修的错，
    不该让整节点失败。用状态机区分"引号内"与"引号外"，字符串内的全角标点保持原样。
    """
    out: list[str] = []
    in_str = False
    esc = False
    for ch in text:
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            continue
        out.append(_FULLWIDTH_STRUCT.get(ch, ch))
    return "".join(out)


def strip_trailing_commas(text: str) -> str:
    """去掉 `,]` / `,}` 这两种尾逗号（JSON 不允许，模型很爱写）。"""
    return re.sub(r",(\s*[}\]])", r"\1", text)


def json_diagnostic(text: str, err: Exception, *, context_lines: int = 3,
                    width: int = 160) -> str:
    """把 JSONDecodeError 变成"行号 + 上下文摘录"的可读诊断（供重试 prompt 携带）。"""
    lineno = int(getattr(err, "lineno", 1) or 1)
    col = int(getattr(err, "colno", 1) or 1)
    lines = text.splitlines()
    lo = max(0, lineno - context_lines - 1)
    hi = min(len(lines), lineno + context_lines)
    excerpt = "\n".join(f"{i + 1:>5}| {lines[i][:width]}" for i in range(lo, hi))
    return f"{err}（第 {lineno} 行第 {col} 列）\n{excerpt}"


def loads_json_tolerant(text: str) -> object:
    """容错解析 LLM 输出的 JSON 对象；全失败时抛 `ValueError`（带诊断）。

    尝试顺序（都确定性、零依赖）：原样 → 去尾逗号 → 结构位全角转半角 → 两者叠加。
    """
    import json as _json

    candidates = [text]
    stripped = strip_trailing_commas(text)
    if stripped != text:
        candidates.append(stripped)
    fixed = fix_structural_punctuation(text)
    if fixed != text:
        candidates.append(fixed)
        candidates.append(strip_trailing_commas(fixed))
    first_err: Exception | None = None
    for cand in candidates:
        try:
            return _json.loads(cand)
        except ValueError as e:  # JSONDecodeError 是 ValueError 子类
            first_err = first_err or e
    raise ValueError("JSON 解析失败：" + json_diagnostic(text, first_err or ValueError("?")))

