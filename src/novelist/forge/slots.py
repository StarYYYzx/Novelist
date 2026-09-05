"""槽位表 + 缺口检测 + 分组（docs/10 §5.3）。

核心原则：**问题由引擎（确定性）决定，候选由模型（LLM）生成**。
本模块只做确定性的部分：槽位定义、缺口检测、分轮分组——零 LLM 调用。

槽位 key = 蓝图路径。两个特殊伪路径：
- `characters[role:protagonist].name` — 按 role 找人物（数组无下标依赖）
- `meta.endgame` — 结局走向（引擎翻译到 volumes 末卷）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .state import Blueprint, get_path

GROUP_LABELS = {
    1: "书级必填",
    2: "世界与规则",
    3: "文风与叙事",
    4: "人物与伏笔",
}


@dataclass
class Slot:
    key: str  # 蓝图路径（或 characters[role:*] 伪路径）
    label: str  # 人类可读名
    level: str = "required"  # required | recommended | optional
    kind: str = "free"  # choice | confirm | free
    ask: str = ""
    candidates_from: str = "template"  # llm | template | enum
    enum: list[str] = field(default_factory=list)
    default: str = ""
    group: int = 1  # 第几轮问
    why: str = ""
    confidence_threshold: float = 0.6  # provenance 低于此值视为低置信缺口


def _craft_ids() -> list[str]:
    """可用题材工艺卡 id（src/novelist/craft/cards/*.md 的文件名）。"""
    try:
        from ..craft.loader import valid_ids

        return valid_ids()
    except Exception:      # 卡目录缺失/损坏不应拖垮槽位表
        return []


def _char_slot(role: str, sub: str) -> str:
    return f"characters[role:{role}].{sub}"


# ---- 槽位表（docs/10 §5.3：必填 8 + 推荐若干）----
def default_slots() -> list[Slot]:
    return [
        # 轮 1：书级必填
        Slot("meta.genre", "流派/模板", "required", "choice", "这本书是什么类型？",
             "template", [], "", 1, "驱动 Genre Pack 装载与全部节点 prompt", 0.5),
        Slot(_char_slot("protagonist", "name"), "主角姓名", "required", "free",
             "主角叫什么？", "llm", [], "", 1, "防性别漂移（B-02 实测）", 0.5),
        Slot(_char_slot("protagonist", "gender"), "主角性别", "required", "choice",
             "主角性别？", "enum", ["male", "female"], "male", 1, "防性别漂移", 0.5),
        Slot("worldview.power_system.mechanic", "金手指/核心机制", "required", "free",
             "金手指或核心机制是什么？", "llm", [], "", 1, "本书最大卖点", 0.5),
        Slot("meta.logline", "核心冲突/卖点", "required", "free",
             "一句话说清卖点与冲突？", "llm", [], "", 1, "驱动全部节点基调", 0.5),
        Slot("meta.scale", "规模", "required", "confirm",
             "规模：X 卷 × Y 章 × Z 字？", "template", [], "", 1, "预算与卷闸门", 0.5),
        # 轮 2：世界与规则
        Slot("worldview.power_system.levels", "境界体系", "required", "choice",
             "境界/等级体系？", "template", [], "", 2, "写入 worldview，驱动战力单调检查", 0.6),
        Slot("worldview.name", "世界名", "recommended", "choice",
             "世界观叫什么？", "llm", [], "", 2, "worldview.name", 0.6),
        Slot("worldview.factions", "势力格局", "recommended", "free",
             "有哪些势力？", "llm", [], "", 2, "factions[]，驱动阵营冲突", 0.6),
        Slot("worldview.rules", "世界铁律", "recommended", "free",
             "世界铁律（不可违背的规则）？", "llm", [], "", 2, "rules[]，一致性引擎消费", 0.6),
        # 轮 3：文风与叙事
        Slot("style.tone", "文风基调", "required", "choice",
             "这本书的基调偏哪种？", "enum", ["热血激昂", "严谨冷肃", "诙谐幽默", "杀伐果断", "温柔细腻"],
             "热血激昂", 3, "写入 style.tone，直接驱动润色 prompt", 0.6),
        Slot("style.pov", "视角", "required", "choice",
             "叙述视角？", "enum", ["第三人称限知（主角视角）", "第三人称全知", "第一人称"],
             "第三人称限知（主角视角）", 3, "style.pov", 0.6),
        Slot("style.tense", "叙述时态", "recommended", "choice",
             "叙述时态？", "enum", ["过去", "现在"], "过去", 3, "style.tense", 0.6),
        Slot("style.narration", "叙事节奏", "recommended", "free",
             "叙事节奏偏好？（如：冲突密集、打脸干脆、升级快 / 慢热铺垫、张弛有度）",
             "llm", [], "", 3, "style.narration，节奏是最该用户拍板的风格维度"
             "（AI 味罚分主要来自段落节奏均匀）", 0.6),
        Slot("style.forbidden_words", "禁用词", "recommended", "free",
             "禁用词（现代词/出戏词）？", "template", [], "", 3, "style.forbidden_words，默认取 Genre Pack", 0.6),
        Slot("style.glossary", "术语表", "recommended", "free",
             "需要登记的专属术语？", "llm", [], "", 3, "style.glossary，防术语漂移", 0.6),
        # 题材工艺卡：把"怎么呈现"固化成硬规范（2026-09-05 教训：风格解释权全交模型
        # → 模型把"系统提示音/叮"当俗套禁用，系统流小说里系统零次发声）
        Slot("style.craft_cards", "题材工艺卡", "recommended", "free",
             "启用哪些题材工艺卡？（多选用顿号/逗号分隔；直接回车=不启用）",
             "enum", _craft_ids(), "", 3,
             "style.craft_cards，规范'怎么呈现'（如系统流的【】发言、单章节奏、伏笔分级）", 0.6),
        # 轮 4：人物与伏笔
        Slot(_char_slot("rival", "name"), "反派设定", "recommended", "free",
             "主要反派/对手？", "llm", [], "", 4, "rival 角色卡", 0.6),
        Slot(_char_slot("love_interest", "name"), "感情线", "recommended", "free",
             "感情线对象？", "llm", [], "", 4, "love_interest 角色卡", 0.6),
        Slot("meta.endgame", "结局走向", "recommended", "free",
             "结局大致走向？", "llm", [], "", 4, "翻译到 volumes 末卷 summary", 0.6),
        Slot("threads", "主线伏笔", "recommended", "free",
             "需要埋的主线伏笔？", "llm", [], "", 4, "threads[]，phase.py payoff 消费", 0.6),
        Slot(_char_slot("protagonist", "core_traits"), "主角性格", "recommended", "free",
             "主角性格三词？", "llm", [], "", 4, "characters[0].core_traits", 0.6),
    ]


def slots_for_genre(pack: dict | None) -> list[Slot]:
    """默认槽位表 + Genre Pack.slots 覆盖（candidates/enum/ask/default/group）。"""
    slots = default_slots()
    if not pack:
        return slots
    overrides = pack.get("slots") or {}
    by_key = {s.key: s for s in slots}
    for key, patch in overrides.items():
        slot = by_key.get(key)
        if slot is None:
            continue
        if "candidates" in patch:
            slot.candidates_from = "template"
            slot.enum = list(patch["candidates"])
        if "enum" in patch:
            slot.candidates_from = "enum"
            slot.enum = list(patch["enum"])
        if "ask" in patch:
            slot.ask = patch["ask"]
        if "default" in patch:
            slot.default = patch["default"]
        if "group" in patch:
            slot.group = int(patch["group"])
    return slots


# ---- 缺口检测 ----
@dataclass
class Gap:
    slot: Slot
    reason: str  # unfilled | low_confidence
    current: Any = None


def _resolve_key(bp: Blueprint, key: str) -> Any:
    """解析槽位 key 为实际值；characters[role:*] 伪路径按角色匹配。"""
    if key.startswith("characters[role:"):
        role = key[len("characters[role:") :].split("]")[0]
        sub = key.split("]", 1)[1].lstrip(".")
        found = next((c for c in bp.section("characters") if c.get("role") == role), None)
        if found is None:
            return None
        if not sub:
            return found
        return get_path(found, sub, None)
    return bp.get(key, None)


def detect_gaps(bp: Blueprint, slots: list[Slot] | None = None) -> list[Gap]:
    """给定蓝图与槽位表 → 待问/待补槽位（确定性，无 LLM）。

    缺口 = 未填（unfilled）或已填但 provenance 低置信（low_confidence，src=llm）。
    按优先级排序：required 未填 > required 低置信 > recommended 未填 > 其余。
    """
    slots = slots or default_slots()
    gaps: list[Gap] = []
    for slot in slots:
        if slot.level == "optional":
            continue
        value = _resolve_key(bp, slot.key)
        filled = _is_filled(value)
        if not filled:
            gaps.append(Gap(slot, "unfilled", value))
            continue
        prov = bp.get_provenance(slot.key)
        src = (prov or {}).get("src")
        conf = (prov or {}).get("confidence", 1.0)
        if prov and src == "llm" and conf < slot.confidence_threshold:
            gaps.append(Gap(slot, "low_confidence", value))
    order = {
        ("required", "unfilled"): 0,
        ("required", "low_confidence"): 1,
        ("recommended", "unfilled"): 2,
        ("recommended", "low_confidence"): 3,
    }
    gaps.sort(key=lambda g: order.get((g.slot.level, g.reason), 9))
    return gaps


def _is_filled(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return len(value) > 0
    return True


# ---- 分轮分组（docs/10 §5.3：3–4 轮 × 2–4 问）----
def group_slots(slots: list[Slot], per_round: int = 4) -> list[tuple[str, list[Slot]]]:
    """按 slot.group 分组；无显式 group 的按 required 优先补进前组。

    返回 [(组名, [Slot…])]，每组 ≤ per_round 问。超出一轮的槽位**顺延到
    下一轮**（H4 修复，2026-09-05：原先 items[:per_round] 静默丢弃第 5 个槽位，
    protagonist.core_traits 因此从未被问过）。
    """
    grouped: dict[int, list[Slot]] = {}
    for s in slots:
        if s.level == "optional":
            continue
        grouped.setdefault(s.group, []).append(s)
    if not grouped:
        return []
    rounds: list[tuple[str, list[Slot]]] = []
    for g in sorted(grouped):
        items = grouped[g]
        label = GROUP_LABELS.get(g, f"第 {g} 轮")
        for i in range(0, len(items), per_round):
            chunk = items[i:i + per_round]
            rounds.append((label if i == 0 else f"{label}（续）", chunk))
    return rounds
