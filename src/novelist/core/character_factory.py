"""角色工厂（ADR-022，M3q）：按需求生产新角色——LLM 草卡 + 确定性闸门。

设计定位（docs/03 ADR-022，2026-09-02 全部拍板）：
- **新事物必须走登记，不允许追认**——JIT 补卡是"名字先出现正文、事后补档"的字面追认，
  与物品侧 supplement_settings（强化符）同构；工厂是人物侧唯一的**正面登记通道**。
- 与世界广播联动（用户口径）：广播先在可及池内找人 → 找不到合适人选才输出结构化缺人
  需求 → 转交工厂生产 → 过确定性闸门 → 自动注册 active → 本事件即可注卡入场（同步）。
- 同步入戏的"薄卡顾虑"由最小卡规格兜底：关系钩子 ≥1（指向已存在角色）、行为规格
  2-3 条（可执行，非 trait 词）、境界 ∈ 境界表、宗门 ∈ 名册——卡薄但合法、可审计。

闸门**零模型调用**：名字查重 / 境界 parse_realm / 宗门名册 / 关系可达 / 配额 ≤2。
每章新角色配额 ≤2（拍板 2）；超额需求进 `bible/character_needs_pending.json` 下章消化。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .llm import LLMMessage, LLMRequest
from .normalize import regroup_factions

NEEDS_PATH = "character_needs_pending.json"   # 相对 bible/
PER_CHAPTER_QUOTA = 2                         # 拍板 2：每章工厂新角色 ≤2
CONFIDENCE = 0.8                              # 工厂产卡置信度（ADR-017 provenance）


@dataclass
class CharacterNeed:
    """结构化缺人需求——广播 v1（ADR-021 未实施，先定 schema）/ CLI / JIT 告警共用。"""

    role: str                    # 职能：执法长老 / 坊市管事 …
    description: str = ""        # 一句话需求全文（CLI 入口即原文）
    realm_hint: str = ""         # 境界倾向（可空，闸门仍以境界表为准）
    faction_hint: str = ""       # 阵营倾向（可空）
    hooks: list[dict] = field(default_factory=list)   # [{"to": "叶岚", "rel": "有过节"}]
    why_existing_fail: str = ""  # 为何可及池内无人可用（广播字段）
    source: str = "manual"       # manual / broadcast / jit_alarm
    vol: int = 0
    ch: int = 0

    def to_dict(self) -> dict:
        return {"role": self.role, "description": self.description,
                "realm_hint": self.realm_hint, "faction_hint": self.faction_hint,
                "hooks": self.hooks, "why_existing_fail": self.why_existing_fail,
                "source": self.source, "vol": self.vol, "ch": self.ch}

    @classmethod
    def from_dict(cls, d: dict) -> "CharacterNeed":
        return cls(
            role=str(d.get("role") or "未指明"),
            description=str(d.get("description") or ""),
            realm_hint=str(d.get("realm_hint") or ""),
            faction_hint=str(d.get("faction_hint") or ""),
            hooks=[h for h in (d.get("hooks") or []) if isinstance(h, dict)],
            why_existing_fail=str(d.get("why_existing_fail") or ""),
            source=str(d.get("source") or "manual"),
            vol=int(d.get("vol", 0) or 0), ch=int(d.get("ch", 0) or 0),
        )


@dataclass
class FactoryReport:
    ok: bool = False
    card: dict | None = None          # 过闸门并注册后的正式卡（复用时=被复用的现有卡）
    rejections: list[str] = field(default_factory=list)   # 各闸门拒绝原因（全程披露）
    queued: bool = False              # True=因配额/闸门进需求队列待下章
    reused: str = ""                  # 非空=本需求经检索复用现有角色（未造新卡，不占配额）


# ---------------------------------------------------------------- 名册读取（纯确定性）

def _read_bible(ws, project_id: str, rel: str):
    p = ws._abs(f"{project_id}/bible/{rel}")
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def _realm_levels(ws, project_id: str) -> list[str]:
    wv = _read_bible(ws, project_id, "worldview.json") or {}
    ps = wv.get("power_system") or {}
    return [str(x) for x in (ps.get("levels") or []) if x]


def _faction_names(ws, project_id: str) -> set[str]:
    """势力名册（`power.faction` 的取值域）。

    批次 2：走 `regroup_factions` 归一——原先直读 `factions[].faction`，而存量盘上
    该字段可能是 `"name：玄剑宗"`（写侧修复前产生的逐字段拍平脏数据），于是名册里
    全是 `name：…` / `type：…` 这类垃圾，模型无从选取、闸门也无从比对。
    """
    wv = _read_bible(ws, project_id, "worldview.json") or {}
    out: set[str] = set()
    for f in regroup_factions(wv.get("factions") or []):
        if f.get("faction"):
            out.add(str(f["faction"]))
    return out


def _taken_names(ws, project_id: str) -> set[str]:
    """全部已登记名字+别名（人物/地点/物品/功法/设定/registry）——名字查重闸门用。"""
    taken: set[str] = set()
    for rel, name_key in (("characters.json", "name"), ("locations.json", "name"),
                          ("items.json", "name"), ("skills.json", "name")):
        for e in (_read_bible(ws, project_id, rel) or []):
            if isinstance(e, dict):
                if e.get(name_key):
                    taken.add(str(e[name_key]))
                for a in (e.get("aliases") or []):
                    taken.add(str(a))
    for s in (_read_bible(ws, project_id, "settings.json") or []):
        if isinstance(s, dict):
            if s.get("term"):
                taken.add(str(s["term"]))
            for kw in (s.get("keywords") or []):
                taken.add(str(kw))
    try:
        from .registry import Registry

        for e in Registry.load(ws, project_id).all_entries():
            taken.add(e.name)
    except Exception:  # noqa: BLE001 - registry 缺失不阻断
        pass
    return taken


def _char_index(ws, project_id: str) -> dict[str, str]:
    """名字/别名 → 角色 id（关系钩子可达性闸门 + 名字→id 解析）。"""
    idx: dict[str, str] = {}
    for c in (_read_bible(ws, project_id, "characters.json") or []):
        if isinstance(c, dict) and c.get("id"):
            if c.get("name"):
                idx[str(c["name"])] = str(c["id"])
            for a in (c.get("aliases") or []):
                idx[str(a)] = str(c["id"])
    return idx


def _produced_this_chapter(ws, project_id: str, vol: int, ch: int) -> int:
    n = 0
    for c in (_read_bible(ws, project_id, "characters.json") or []):
        if (isinstance(c, dict) and c.get("provenance", {}).get("origin") == "factory"):
            fa = c.get("first_appear") or {}
            if int(fa.get("vol", 0) or 0) == vol and int(fa.get("ch", 0) or 0) == ch:
                n += 1
    return n


# ---------------------------------------------------------------- 需求队列

def queue_need(ws, project_id: str, need: CharacterNeed) -> None:
    """需求入队（配额超限 / JIT 告警转介 / 广播提名暂存共用）。"""
    p = ws._abs(f"{project_id}/bible/{NEEDS_PATH}")
    items = []
    if p.exists():
        try:
            items = json.loads(p.read_text(encoding="utf-8")) or []
        except (ValueError, OSError):
            items = []
    items.append(need.to_dict())
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def load_queue(ws, project_id: str) -> list[CharacterNeed]:
    p = ws._abs(f"{project_id}/bible/{NEEDS_PATH}")
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8")) or []
    except (ValueError, OSError):
        return []
    return [CharacterNeed.from_dict(d) for d in raw if isinstance(d, dict)]


def _save_queue(ws, project_id: str, needs: list[CharacterNeed]) -> None:
    p = ws._abs(f"{project_id}/bible/{NEEDS_PATH}")
    p.write_text(json.dumps([n.to_dict() for n in needs], ensure_ascii=False, indent=2),
                 encoding="utf-8")


# ---------------------------------------------------------------- 闸门（零调用）

def check_card(card: dict, *, ws, project_id: str) -> list[str]:
    """确定性闸门。返回拒绝原因列表（空 = 通过）。任一不过即整卡拒收。"""
    reasons: list[str] = []
    name = str(card.get("name") or "").strip()
    if not name:
        return ["无名字"]
    if name in _taken_names(ws, project_id):
        reasons.append(f"名字撞已登记名册：{name}")
    levels = _realm_levels(ws, project_id)
    level = str((card.get("power") or {}).get("level") or "").strip()
    from .worldstate import parse_realm

    if not level or parse_realm(level, levels) is None:
        reasons.append(f"境界不在体系内（{level!r} ∉ {levels}）")
    fac = str((card.get("power") or {}).get("faction") or "").strip()
    if not fac or fac not in _faction_names(ws, project_id):
        reasons.append(f"宗门不在名册（{fac!r}）")
    if card.get("gender") not in ("male", "female"):
        reasons.append(f"性别非法（{card.get('gender')!r}，须 male/female）")
    rules = [str(r) for r in (card.get("behavior_rules") or []) if str(r).strip()]
    if not 2 <= len(rules) <= 3:
        reasons.append(f"行为规格须 2-3 条可执行规则（现 {len(rules)} 条）")
    rels = card.get("relationships") or []
    if len(rels) < 1:
        reasons.append("关系钩子 <1（最小卡规格要求至少指向一名已存在角色）")
    else:
        idx = _char_index(ws, project_id)
        for r in rels:
            if isinstance(r, dict) and str(r.get("target") or "") not in idx:
                reasons.append(f"关系指向不存在角色：{r.get('target')!r}")
                break
    return reasons


def _normalize_card(card: dict, *, ws, project_id: str, need: CharacterNeed,
                    vol: int, ch: int) -> dict:
    """过闸门后的卡规范化：关系名→id、字段收编、provenance 落章。"""
    idx = _char_index(ws, project_id)
    rels = []
    for r in card.get("relationships") or []:
        if isinstance(r, dict) and str(r.get("target") or "") in idx:
            rels.append({"target": idx[str(r["target"])],
                         "type": str(r.get("type") or "关联")[:24]})
    n_existing = len(_read_bible(ws, project_id, "characters.json") or [])
    return {
        "id": f"char:fac{n_existing + 1}",
        "name": str(card["name"]).strip(),
        "aliases": [str(a) for a in (card.get("aliases") or []) if a][:4],
        "gender": card.get("gender"),
        "age": card.get("age"),
        "core_traits": [str(x) for x in (card.get("core_traits") or [])][:5],
        "power": {"level": str(card["power"]["level"]).strip(),
                  "faction": str(card["power"]["faction"]).strip()},
        "role": str(card.get("role") or need.role)[:40],
        "behavior_rules": [str(r) for r in card.get("behavior_rules") if str(r).strip()][:3],
        "relationships": rels,
        "arc": str(card.get("arc") or "")[:120],
        "first_appear": {"vol": vol, "ch": ch},
        "status": "active",           # 拍板 4：过闸门自动注册 active，事后人工可改
        "provenance": {"origin": "factory", "channel": need.source,
                       "confidence": CONFIDENCE},
    }


# ---------------------------------------------------------------- 生产管线

_PROMPT = (
    "你是人物设定师。按下面的**缺人需求**生产一张新角色卡（修仙长篇）。\n"
    "需求：{desc}\n"
    "职能：{role}；境界倾向：{realm}；阵营倾向：{faction}\n"
    "现有角色（关系钩子必须指向其中之一）：{cast}\n"
    "境界表（power.level 必须出自此表，可带层次如「金丹中期」）：{levels}\n"
    "宗门/势力名册（power.faction 必须出自此表）：{factions}\n\n"
    "输出单个 JSON 对象，字段：\n"
    '{{"name": "姓名（不与任何现有角色重名）", "aliases": [], "gender": "male|female",\n'
    '"age": 数字, "core_traits": ["性格1","性格2","性格3"],\n'
    '"power": {{"level": "境界", "faction": "宗门"}}, "role": "{role}",\n'
    '"behavior_rules": ["可执行行为规格1", "行为规格2", "行为规格3"],\n'
    '"relationships": [{{"target": "现有角色名", "type": "关系一句话"}}],\n'
    '"arc": "一句话人物弧线"}}\n'
    "硬性要求：relationships 至少 1 条且 target 是上面现有角色之一；behavior_rules 2-3 条"
    "（写可执行的动作/立场规则，不要性格形容词）。只输出 JSON。"
)


def produce(ws, project_id: str, need: CharacterNeed, provider, *,
            vol: int, ch: int) -> FactoryReport:
    """生产一个新角色：LLM 草卡 → 确定性闸门 → 注册 active。闸门/配额不过则拒收或入队。"""
    report = FactoryReport()
    # 配额闸门：本章工厂产出已满 → 需求入队（拍板 2）
    if _produced_this_chapter(ws, project_id, vol, ch) >= PER_CHAPTER_QUOTA:
        queue_need(ws, project_id, need)
        report.queued = True
        report.rejections.append(f"本章工厂配额已满（{PER_CHAPTER_QUOTA}），需求转队列")
        return report
    if provider is None:
        queue_need(ws, project_id, need)
        report.queued = True
        report.rejections.append("无可用 provider，需求转队列")
        return report

    cast = _char_index(ws, project_id)
    if not cast:
        report.rejections.append("bible 无任何已登记角色，关系钩子无从指向")
        return report
    levels = _realm_levels(ws, project_id)
    factions = sorted(_faction_names(ws, project_id))
    prompt = _PROMPT.format(
        desc=need.description or need.role, role=need.role,
        realm=need.realm_hint or "不限", faction=need.faction_hint or "不限",
        cast="、".join(list(cast)[:20]), levels="、".join(levels),
        factions="、".join(factions[:12]))
    try:
        res = provider.complete(LLMRequest(
            messages=[LLMMessage(role="user", content=prompt)],
            max_tokens_out=800, temperature=0.5, response_format="json_object",
            thinking=False))  # 生成类：草卡生成，关思考
        if res.blocked or not (res.content or "").strip():
            queue_need(ws, project_id, need)
            report.queued = True
            report.rejections.append("LLM 草卡失败（blocked/空），需求转队列")
            return report
        card = json.loads(res.content.strip())
        if not isinstance(card, dict):
            raise ValueError("草卡不是 JSON 对象")
    except Exception:  # noqa: BLE001 - LLM/解析失败不阻断生成，需求留队
        queue_need(ws, project_id, need)
        report.queued = True
        report.rejections.append("LLM 草卡解析失败，需求转队列")
        return report

    reasons = check_card(card, ws=ws, project_id=project_id)
    if reasons:
        report.rejections = reasons       # 闸门拒绝**不入队**——坏卡重试无意义，人工看原因
        return report
    new_card = _normalize_card(card, ws=ws, project_id=project_id, need=need, vol=vol, ch=ch)
    p = ws.bible_path(project_id, "characters")
    chars = _read_bible(ws, project_id, "characters.json") or []
    chars.append(new_card)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(chars, ensure_ascii=False, indent=2), encoding="utf-8")
    # 新人物进 worldstate（init_from_bible 不覆盖已有状态）
    try:
        from .worldstate import init_from_bible

        init_from_bible(ws, project_id)
    except Exception:  # noqa: BLE001
        pass
    report.ok, report.card = True, new_card
    return report


# ---------------------------------------------------------------- 检索复用（needs resolver，2026-09-04 用户拍板）
#
# 需求先在现有卡池里找复用，找不到才走工厂造卡——防止"池里明明有未登场角色，
# 工厂又造一个同职能新卡"。链路：确定性打分 → top-3 候选 → LLM 从严判定。
# 复用不占工厂配额（没有新卡），判定失败/无候选/无 provider 一律落回原工厂路径。


def _card_text(card: dict) -> str:
    """卡的全文本投影（打分用）：名字/别名/职能/性格/行为规格/弧线/境界/阵营。"""
    p = card.get("power") or {}
    parts = [card.get("name"), *(card.get("aliases") or []), card.get("role"),
             *(card.get("core_traits") or []), *(card.get("behavior_rules") or []),
             card.get("arc"), p.get("level"), p.get("faction")]
    return "".join(str(x) for x in parts if x)


def score_card_for_need(card: dict, need: CharacterNeed) -> tuple[float, list[str]]:
    """确定性打分：需求各字段对卡文本的命中。返回 (分数, 命中说明)。"""
    score = 0.0
    hits: list[str] = []
    text = _card_text(card)
    power = card.get("power") or {}

    def _grams(s: str, n: int = 2) -> list[str]:
        s = re.sub(r"\s+", "", str(s or ""))
        return [s[i:i + n] for i in range(0, max(len(s) - n + 1, 0), 1)]

    # 1) 职能命中（权重最高）：need.role 的 2-gram 在卡文本中的覆盖率
    grams = _grams(need.role)
    if grams:
        cov = sum(1 for g in grams if g in text) / len(grams)
        if cov >= 0.5:
            score += 1.0 + cov            # 1.0 ~ 2.0
            hits.append(f"职能≈{cov:.0%}")
    # 2) 阵营倾向命中 power.faction
    if need.faction_hint and str(need.faction_hint) in str(power.get("faction") or ""):
        score += 2.0
        hits.append(f"阵营={need.faction_hint}")
    # 3) 境界倾向前缀命中 power.level
    if need.realm_hint:
        r = re.sub(r"\s+", "", str(need.realm_hint))
        lv = re.sub(r"\s+", "", str(power.get("level") or ""))
        if r and (r in lv or lv in r):
            score += 1.0
            hits.append(f"境界≈{power.get('level')}")
    # 4) 需求描述对卡文本的 2-gram 覆盖（低权重，只做加分不做门槛）
    grams = _grams(need.description)
    if len(grams) >= 3:
        cov = sum(1 for g in grams if g in text) / len(grams)
        if cov >= 0.2:
            score += min(cov, 1.0)
            hits.append(f"描述≈{cov:.0%}")
    # 5) 关系钩子牵引：need.hooks 指向的角色与本卡存在关系边
    idx = {str(card.get("name", ""))}
    idx |= {str(a) for a in (card.get("aliases") or [])}
    for h in need.hooks:
        if str(h.get("to") or "") in idx:
            score += 1.0
            hits.append(f"钩子→{h.get('to')}")
            break
    return score, hits


def _judge_reuse(provider, need: CharacterNeed, cands: list[dict]) -> tuple[str, str]:
    """LLM 从严判定：top 候选是否满足需求。返回 (复用名或空, 理由)。任何异常返回空。"""
    lines = []
    for c in cands:
        p = c.get("power") or {}
        rels = "；".join(f"{r.get('target')}:{r.get('type')}" for r in (c.get("relationships") or [])
                         if isinstance(r, dict))
        lines.append(
            f"- {c.get('name')}｜职能:{c.get('role') or '未指明'}｜境界:{p.get('level')}"
            f"｜阵营:{p.get('faction')}｜性格:{'、'.join(str(x) for x in (c.get('core_traits') or [])[:3])}"
            f"｜关系:{rels or '无'}")
    prompt = (
        "你是选角导演。判断**现有角色**能否满足下面的缺人需求（避免重复造卡）。\n"
        f"【缺人需求】职能:{need.role}｜描述:{need.description}\n"
        f"境界倾向:{need.realm_hint or '不限'}｜阵营倾向:{need.faction_hint or '不限'}\n"
        f"为何现有池不行（广播判定，仅供参考）:{need.why_existing_fail or '无'}\n"
        "【候选角色】\n" + "\n".join(lines) + "\n\n"
        "从严判定：候选必须能承担需求的职能（含境界量级匹配），性格可塑但职能不可塑；"
        "拿不准一律判不满足。只输出 JSON："
        '{"reuse": "角色名或null", "reason": "一句话理由"}')
    try:
        res = provider.complete(LLMRequest(
            messages=[LLMMessage(role="user", content=prompt)],
            max_tokens_out=200, temperature=0.2, response_format="json_object",
            thinking=True))  # 判断类：复用人选从严判定，开思考
        import json as _json

        d = _json.loads(res.content or "{}")
        name = str(d.get("reuse") or "").strip()
        cand_names = {str(c.get("name") or "") for c in cands}
        if name and name in cand_names:
            return name, str(d.get("reason") or "")
    except Exception:  # noqa: BLE001 - 判定失败从严落回工厂
        pass
    return "", "LLM 判定失败/未通过"


def try_reuse(ws, project_id: str, need: CharacterNeed, provider, *,
              vol: int, ch: int) -> FactoryReport:
    """需求检索复用：打分 → top-3 → LLM 判定 → 满足则登记 serves_needs 回链。

    复用成功返回 ok=True + reused=名字（未造新卡，不占工厂配额）；
    未复用返回 ok=False，调用方应继续走 `produce()` 原路径。
    `serves_needs` 回链写在卡上（ADR-016 文件=事实源），供调度层参考与审计。
    """
    report = FactoryReport()
    chars = [c for c in (_read_bible(ws, project_id, "characters.json") or [])
             if isinstance(c, dict) and c.get("name")]
    scored = []
    for c in chars:
        s, hits = score_card_for_need(c, need)
        if s >= 4.0:                      # 门槛：阵营+境界压线（3.0）不够，须职能级命中
            scored.append((s, hits, c))
    scored.sort(key=lambda t: -t[0])
    top = scored[:3]
    if not top:
        report.rejections.append("检索复用：无候选（打分门槛 4.0 未命中）")
        return report
    report.card = top[0][2]
    if provider is None:
        report.rejections.append("检索复用：无 provider 不可判定，转工厂")
        return report
    name, reason = _judge_reuse(provider, need, [c for _, _, c in top])
    if not name:
        report.rejections.append(f"检索复用：{reason}（候选："
                                 + "、".join(str(c.get("name")) for _, _, c in top) + "）")
        return report
    # 复用成立：写 serves_needs 回链（按 id 定位原卡，落盘）
    for c in chars:
        if str(c.get("name")) == name:
            c.setdefault("serves_needs", []).append(
                {"role": need.role, "description": need.description,
                 "vol": vol, "ch": ch, "source": need.source})
            ws.write_json(ws.bible_path(project_id, "characters"), chars)  # noqa: SLF001
            report.ok = True
            report.reused = name
            report.rejections.append(f"检索复用：{name}（{reason or '满足需求'}）")
            return report
    report.rejections.append("检索复用：判定名与卡不符，转工厂")
    return report


def drain_queue(ws, project_id: str, provider, *, vol: int, ch: int,
                limit: int = PER_CHAPTER_QUOTA) -> list[FactoryReport]:
    """章前消化需求队列（事件循环调用点）：先检索复用，未复用才按配额生产。

    复用不占工厂配额（没有新卡）；JIT 告警（source=jit_alarm）点名具体缺失名字，
    池内必然没有（缺卡判定已查别名），跳过复用直接生产。
    """
    reports: list[FactoryReport] = []
    needs = load_queue(ws, project_id)
    if not needs:
        return reports
    remain: list[CharacterNeed] = []
    quota = max(PER_CHAPTER_QUOTA - _produced_this_chapter(ws, project_id, vol, ch), 0)
    produced = 0
    for need in needs:
        # 1) 检索复用（用户拍板 2026-09-04）：零成本消需求，不占配额
        reuse_note: list[str] = []
        if need.source != "jit_alarm" and provider is not None:
            try:
                r = try_reuse(ws, project_id, need, provider, vol=vol, ch=ch)
            except Exception:  # noqa: BLE001 - 复用任何异常都落回工厂
                r = None
            if r is not None and r.ok:
                reports.append(r)
                continue
            if r is not None:
                reuse_note = list(r.rejections)   # 复用尝试留痕，不随 produce 报告丢失
        # 2) 工厂生产（受配额约束）
        if produced < min(limit, quota):
            rep = produce(ws, project_id, need, provider, vol=vol, ch=ch)
            rep.rejections = reuse_note + rep.rejections
            reports.append(rep)
            if rep.ok:
                produced += 1
            else:
                remain.append(need)
        else:
            remain.append(need)
    _save_queue(ws, project_id, remain)
    return reports


def need_from_text(text: str, *, vol: int = 0, ch: int = 0,
                   source: str = "manual") -> CharacterNeed:
    """一句话需求 → CharacterNeed（CLI 入口；职能粗提取，精确语义由草卡 prompt 承接）。"""
    text = text.strip()
    role = "未指明"
    m = re.search(r"(?:需要|想要|来)(一?[名位个])?([\u4e00-\u9fa5]{2,8}?)[，,。；;]", text)
    if m:
        role = m.group(2)
    return CharacterNeed(role=role, description=text, source=source, vol=vol, ch=ch)
