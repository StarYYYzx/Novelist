"""世界广播选角（ADR-021，人物一致性栈第 0 层）——事件级"谁该在场"的 LLM 推理。

现状（ADR-020）：事件选角是确定性的——`declared_cast`（细纲"出场人物"行）+ 字面兜底
（事件文本命中 ≤2）。两个缺口：细纲 key_events 一句话粒度多数不含出场人物 → 字面兜底
捞不到"职能上该在场"的人；且选角是 N3 调度 / N4 视角的上游——人选错，后面全错。

本模块：事件循环注卡前 +1 次 LLM 调用做**选角推理**（广播），产出名单过**确定性校验**
（五条硬约束，防模型乱来）后落盘 `memory/castings/`。模型只许从**可及池**（active −
dead − 闭关/失踪/被囚/渡劫，与 R-STATE 同源）里挑人；认为池内无人合适时，输出结构化
**缺人需求**交 ADR-022 角色工厂生产（source=broadcast 入队，章前 drain 消化）。

失败纪律：任何异常（provider 挂/超时/解析失败）→ 返回 None，调用方静默回退确定性选角，
绝不阻断生成（与 ADR-020 全部新增调用同一纪律）。

拍板记录（2026-09-02，用户拍板 + ★ 推荐默认）：
- ① 名单 ⊇ 细纲声明（求并且，细纲不可删）★；② 新人必经工厂（已拍板）；③ 独立成次 ★；
  ④ 落盘 castings/ ★。v1 落地：produce_chapter 默认 False（显式开启），真机验证后转 True。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .llm import LLMMessage, LLMRequest

CAST_CAP = 6            # 单事件出场上限（ADR-021 校验 5）
CLOSED_CAP = 3          # 封闭场景出场上限（B4，ADR-021 v2——私密/单独/密室类名单从严）
POOL_CAP = 40           # 全量进池上限，超出走 RAG 预筛（P1，本书 ~20 角色不触发）
REASON_CATEGORIES = ("职能必需", "关系牵引", "伏笔相关", "动机主动")
_BLOCKED_STATUS = ("dead", "unknown")   # bible status 应然侧过滤

# 封闭场景词表（B4 确定性信号）：命中 → 名单收敛 + 禁关系牵引加戏。
# 保守取"私密会面"类词，宁可少加戏不误放；子串匹配，词不宜过短。
_CLOSED_WORDS = (
    "密室", "密谈", "密会", "秘议", "单独", "独处", "私下", "私语", "私会",
    "召见", "传召", "寝殿", "闺房", "静室", "禁地", "夜探", "夜话",
    "内堂", "屏退", "无第三人在场",
)


@dataclass
class CastMember:
    name: str                    # bible 角色名（必须在可及池内）
    reason_category: str = ""    # 四类之一：职能必需 / 关系牵引 / 伏笔相关 / 动机主动
    reason: str = ""


@dataclass
class CastNeed:
    """缺人需求（→ character_factory.CharacterNeed 同构，source=broadcast）。"""

    role: str = ""
    description: str = ""
    realm_hint: str = ""
    faction_hint: str = ""
    hooks: list = field(default_factory=list)   # [{"to": 名字, "rel": 关系}]
    why_existing_fail: str = ""


@dataclass
class CastDecision:
    members: list = field(default_factory=list)      # [CastMember]
    needs: list = field(default_factory=list)        # [CastNeed]
    raw: str = ""                                    # 模型原始输出（留痕）
    alarms: list = field(default_factory=list)       # 校验告警（[str]）

    @property
    def names(self) -> list[str]:
        return [m.name for m in self.members]


# ---------------------------------------------------------------- 可及池（确定性，零模型调用）


def _read_worldstate(ws, project_id: str) -> dict | None:
    p = ws._abs(f"{project_id}/bible/worldstate.json")
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def available_pool(chars: list[dict], worldstate: dict | None = None) -> list[dict]:
    """可及角色池 = bible 卡剔除不可出场者（ADR-021 校验 2 的数据源）。

    剔除判据（R-STATE 同源，确定性过滤，不靠模型记）：
    - worldstate.characters[id].dead == True（实然已死）；
    - worldstate.characters[id].unavailable_until > time.now（闭关/失踪/被囚/渡劫中）；
    - bible status ∈ {dead, unknown}（应然侧，worldstate 缺席时的兜底）。
    """
    ws_chars = ((worldstate or {}).get("characters") or {})
    ws_time = ((worldstate or {}).get("time") or {}).get("now")
    out: list[dict] = []
    for c in chars:
        if not isinstance(c, dict) or not c.get("id"):
            continue
        cid = str(c["id"])
        wc = ws_chars.get(cid) or {}
        if wc.get("dead"):
            continue
        until = wc.get("unavailable_until")
        if until is not None and ws_time is not None and int(until) > int(ws_time):
            continue
        if str(c.get("status") or "active") in _BLOCKED_STATUS:
            continue
        out.append(c)
    return out


# ---------------------------------------------------------------- 广播 prompt


def _one_line(c: dict, names: dict | None = None) -> str:
    """池内一行：名字＋称谓/职务＋宗门/境界＋特质＋关系锚（B2，ADR-021 v2）。

    原只给 名字/宗门境界/特质 → 广播 e3 报"青云子=掌门"在输入里**无据可循**
    （模型推断撞对）。v2 补两类可溯源字段：
    - aliases（称谓/职务——"掌门/老祖/门主"是职能必需类理由的锚）；
    - relationships 前 2 条（关系牵引类理由从此有据；target 是 char:xxx id，
      由 `names`（id→名）回查成人名）。
    """
    name = str(c.get("name") or c.get("id") or "?")
    power = c.get("power") if isinstance(c.get("power"), dict) else {}
    lvl = power.get("level")
    fac = power.get("faction")
    bits = [name]
    aliases = [str(a) for a in (c.get("aliases") or []) if str(a) and str(a) != name][:2]
    if aliases:
        bits.append("（" + "、".join(aliases) + "）")
    if fac or lvl:
        bits.append("／".join(x for x in (fac, lvl) if x))
    traits = c.get("core_traits") or []
    if traits:
        bits.append("特质：" + "、".join(str(t) for t in traits[:3]))
    rels = c.get("relationships") or []
    if isinstance(rels, list):
        anchors = []
        for r in rels[:2]:
            if not isinstance(r, dict) or not r.get("type"):
                continue
            tgt = str(r.get("target") or "")
            if names and tgt in names:
                tgt = str(names[tgt])
            rel = str(r["type"])
            if len(rel) > 12:
                rel = rel[:12] + "…"
            anchors.append(f"与{tgt}（{rel}）")
        if anchors:
            bits.append("关系：" + "、".join(anchors))
    return "、".join(bits)


def build_pool_block(pool: list[dict], names: dict | None = None) -> str:
    return "\n".join(f"- {_one_line(c, names)}" for c in pool[:POOL_CAP])


def is_closed_scene(*texts: str) -> bool:
    """B4 确定性判定：任一文本命中封闭词表 → 该事件是封闭/私密场景。

    供广播收敛名单（cap 3 + 禁关系牵引加戏）；开放公开场景不受影响。
    """
    pool = "\n".join(t for t in texts if t)
    return any(w and w in pool for w in _CLOSED_WORDS)


BROADCAST_PROMPT = """你是这部小说的选角导演。判断"这一场戏谁该在场"，只做选角，不写情节。

【本事件】{ev_text}

【前情接缝】（上一事件末尾发生了什么，保证在场感连续）
{seam}

【时间地点】故事内第 {time_now} 天（{time_text}），地点以事件内容为准，事件没提就不写。

【场景开放度】（决定名单该松还是该严——封闭场景**禁止**关系牵引/动机主动加人）
{openness}

【细纲已声明出场】（规划层意图，**必须全部保留**，可补充不可删减）
{declared}

【上一事件出场者】（若与细纲冲突以细纲为准）
{prev}

【可及角色池】（只能从下列角色中选，**绝不编造新名字**；角色可能不在场、不在宗门，选人请考虑合理性）
{pool_block}

请判断：
1. present：本事件应当出场的人。每人的 reason_category 限下列四类之一：
   - 职能必需：这场的场景/职能要求他必须在（如大比必有裁判、宗门议事必有长老）
   - 关系牵引：与事件主角/在场者有强关系，理应牵涉（如好友/宿敌/恩师）
   - 伏笔相关：他身上有本事件要推进的伏笔/线索
   - 动机主动：他本人有动机主动介入此事
   没提到的角色不要强行加戏；一般 2-4 人，最多 {cast_cap} 人。
2. needs：若你认为池内没有合适人选承担某个必需职能（如"宗门大比缺一名元婴裁判"），
   输出一条缺人需求；池内能找到人选就不要输出 needs。

只输出 JSON（不要其它文字）：
{{"present": [{{"name": "<池内角色名>", "reason_category": "<四类之一>", "reason": "<一句话，为什么该在场>"}}],
  "needs": [{{"role": "<职能>", "realm_hint": "<境界倾向，可空>", "faction_hint": "<阵营倾向，可空>",
              "relation_hook": "<与在场者关系钩子，可空>", "why_existing_fail": "<为何池内无人可用>"}}]}}"""


# ---------------------------------------------------------------- 解析（防造名）


def parse_decision(content: str, pool_names: set[str]) -> tuple[list[CastMember], list[CastNeed], list[str]]:
    """解析模型输出。造名（不在池）拒绝 + 告警，不崩。返回 (members, needs, alarms)。"""
    alarms: list[str] = []
    try:
        data = json.loads(content)
    except ValueError:
        # 模型偶发把 JSON 包在 ```json 块里
        import re as _re
        m = _re.search(r"```(?:json)?\s*(\{.*\})\s*```", content or "", _re.S)
        if not m:
            return [], [], ["广播输出非 JSON，解析失败"]
        try:
            data = json.loads(m.group(1))
        except ValueError:
            return [], [], ["广播输出非 JSON，解析失败"]
    if not isinstance(data, dict):
        return [], [], ["广播输出结构异常"]

    members: list[CastMember] = []
    for p in (data.get("present") or []):
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "").strip()
        if not name:
            continue
        if name not in pool_names:
            alarms.append(f"广播点名「{name}」不在可及池（自造名/不可出场），已拒绝")
            continue
        cat = str(p.get("reason_category") or "")
        if cat not in REASON_CATEGORIES:
            cat = "职能必需" if not cat else f"{cat}"
        members.append(CastMember(name=name, reason_category=cat,
                                  reason=str(p.get("reason") or "")))

    needs: list[CastNeed] = []
    for n in (data.get("needs") or []):
        if not isinstance(n, dict):
            continue
        role = str(n.get("role") or "").strip()
        if not role:
            continue
        hook = str(n.get("relation_hook") or "").strip()
        needs.append(CastNeed(
            role=role,
            description=f"广播缺人需求：{role}",
            realm_hint=str(n.get("realm_hint") or "").strip(),
            faction_hint=str(n.get("faction_hint") or "").strip(),
            hooks=[{"to": hook, "rel": "待定"}] if hook else [],
            why_existing_fail=str(n.get("why_existing_fail") or "").strip(),
        ))
    if needs and not members:
        alarms.append("广播只给了缺人需求未给出场名单")
    return members, needs, alarms


# ---------------------------------------------------------------- 确定性校验（五条硬约束）


def validate_names(names: list[str], *, declared: list[str], pool: list[str],
                   text_hits: list[str], cap: int = CAST_CAP) -> tuple[list[str], list[str]]:
    """校验并修正名单。返回 (最终名单, 告警)。

    1. 名单 ⊇ 细纲声明（规划层意图不可被广播删）——缺则补 + 告警；
    2. 名单 ⊆ 可及池——广播点名不在池者剔除 + 告警（解析期已拒，此处兜底）；
    3. 事件文本字面命中必须涵盖——缺则补（防广播漏读事件正文）；
    4. 上限 cap（开放 CAST_CAP / 封闭 CLOSED_CAP）——超出按 细纲声明 > 职能必需 > 文本命中
       裁（超出部分告警）。
    """
    alarms: list[str] = []
    pool_set, name_set = set(pool), set(names)
    declared_set, hit_set = set(declared), set(text_hits)

    # 2. 池外剔除（兜底：成员可能在池内名字之外——一般不会，防御）
    off = name_set - pool_set
    if off:
        alarms.append(f"广播名单含池外角色 {sorted(off)}，已剔除")
        name_set -= off

    # 1. 细纲声明补回
    missing_declared = declared_set - name_set
    if missing_declared:
        alarms.append(f"广播遗漏细纲声明 {sorted(missing_declared)}，已强制保留")
        name_set |= missing_declared

    # 3. 文本命中补回
    missing_hits = hit_set - name_set
    if missing_hits:
        alarms.append(f"广播遗漏事件文本命中 {sorted(missing_hits)}，已补回")
        name_set |= missing_hits

    final = list(name_set)
    # 4. 上限裁剪：细纲声明 > 文本命中 优先保留，其余按原顺序截断
    if len(final) > cap:
        keep_pri = list(declared_set | (hit_set & set(final)))
        rest = [n for n in final if n not in keep_pri]
        kept = keep_pri[:cap]
        if len(kept) < cap:
            kept += rest[:cap - len(kept)]
        dropped = [n for n in final if n not in kept]
        alarms.append(f"名单超上限({len(final)}>{cap})，裁掉 {dropped}")
        final = kept
    return final, alarms


# ---------------------------------------------------------------- 主调用（含落盘）


def _castings_path(ws, project_id: str, vol: int, ch: int, idx: int):
    return ws._abs(f"{project_id}/memory/castings/v{vol}-c{ch}-e{idx}.json")


def save_casting(ws, project_id: str, vol: int, ch: int, idx: int,
                 decision: CastDecision, *, closed: bool | None = None) -> None:
    """落盘广播决定与理由（ADR-016：文件即事实源，可审"这场戏他为什么在"）。"""
    p = _castings_path(ws, project_id, vol, ch, idx)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "vol": vol, "ch": ch, "event_index": idx,
        "closed_scene": closed,                       # B4：名单从严的判定留痕
        "present": [{"name": m.name, "reason_category": m.reason_category,
                     "reason": m.reason} for m in decision.members],
        "needs": [n.__dict__ for n in decision.needs],
        "alarms": decision.alarms,
        "raw": decision.raw,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def broadcast_cast(
    ws,
    project_id: str,
    provider,
    *,
    vol: int,
    ch: int,
    idx: int,
    ev_text: str,
    seam: str = "",
    declared: list[str] | None = None,
    prev: list[str] | None = None,
    worldstate: dict | None = None,
    pool: list[dict] | None = None,
    text_hits: list[str] | None = None,
    system_prompt: str | None = None,
    max_tokens: int = 700,
    closed: bool | None = None,
) -> CastDecision | None:
    """一次广播：可及池 → prompt → LLM → 解析 → 校验 → 落盘 → needs 入队。

    返回 None = 广播不可用/失败（调用方静默走确定性选角，绝不阻断）。

    `closed`（B4）：None → 由事件文本自动判定封闭场景（词表命中）；
    True → 名单从严（CLOSED_CAP + 只许"职能必需"提名）；False → 开放场景正常选角。
    """
    if provider is None:
        return None
    from .director import load_characters  # 延迟导入避免环

    chars = load_characters(ws, project_id)
    if not chars:
        return None
    if pool is None:
        pool = available_pool(chars, worldstate)
    pool_names = {str(c.get("name") or "") for c in pool if c.get("name")}
    if not pool_names:
        return None
    # id→名（B2：关系锚的 target 是 char:xxx，回查成人名再渲染）
    names_map = {str(c.get("id") or ""): str(c.get("name") or "") for c in chars if c.get("id")}

    wst = worldstate or _read_worldstate(ws, project_id) or {}
    time_now = ((wst.get("time") or {}).get("now")) or 0
    time_text = ((wst.get("time") or {}).get("origin_text")) or ""

    # B4：封闭场景判定（自动 = 事件/接缝文本命中词表；显式传参可覆盖）
    if closed is None:
        closed = is_closed_scene(ev_text or "", seam or "")
    cap = CLOSED_CAP if closed else CAST_CAP
    openness = (
        f"**封闭场景**（私密/单独/密室/召见类）：只保留职能上不得不场的人"
        f"（如传召双方、随行护卫），禁止因关系牵引或动机主动加人，最多 {cap} 人。"
        if closed else
        f"开放场景（公开场合）：按职能/关系/伏笔/动机正常判断在场，最多 {cap} 人。")

    decl = declared or []
    prev_names = prev or []
    prompt = BROADCAST_PROMPT.format(
        ev_text=(ev_text or "")[:600], seam=(seam or "")[:300],
        time_now=time_now, time_text=time_text,
        openness=openness,
        declared="、".join(decl) if decl else "（无）",
        prev="、".join(prev_names) if prev_names else "（本章首个事件）",
        pool_block=build_pool_block(pool, names_map), cast_cap=cap)
    try:
        res = provider.complete(LLMRequest(
            messages=[
                LLMMessage(role="system",
                           content=system_prompt or "你是小说的选角导演，只做事件选角推理。"),
                LLMMessage(role="user", content=prompt)],
            max_tokens_out=max_tokens,
            temperature=0.2,
            response_format="json_object",
        ))
    except Exception:  # noqa: BLE001 - 广播失败静默降级（与 ADR-020 同纪律）
        return None
    if res.blocked or not (res.content or "").strip():
        return None

    members, needs, parse_alarms = parse_decision(res.content, pool_names)
    if closed:
        # B4：封闭场景只许"职能必需"提名——关系牵引/动机主动/伏笔相关的加戏一律拒绝
        kept, dropped = [], []
        for m in members:
            (kept if m.reason_category == "职能必需" else dropped).append(m)
        if dropped:
            parse_alarms.append("封闭场景拒绝非职能必需加戏："
                                + "、".join(m.name for m in dropped))
        members = kept
    if not members and not needs:
        return None  # 解析彻底失败 → 降级

    # 校验：名字层面（细纲/文本命中/上限 cap）
    final_names, val_alarms = validate_names(
        [m.name for m in members],
        declared=[n for n in decl if n in pool_names],
        pool=list(pool_names),
        text_hits=[h for h in (text_hits or []) if h in pool_names],
        cap=cap)

    # 成员按最终名单过滤并保留理由
    by_name = {m.name: m for m in members}
    kept_members = []
    for nm in final_names:
        m = by_name.get(nm)
        if m is None:  # 校验补回的（细纲声明/文本命中）
            kept_members.append(CastMember(name=nm, reason_category="职能必需",
                                           reason="确定性补回（细纲声明/文本命中）"))
        else:
            kept_members.append(m)
    # 顺序稳定：先模型给出的、后补回的保持名单原有相对序
    order = {n: i for i, n in enumerate(final_names)}
    kept_members.sort(key=lambda m: order.get(m.name, 0))

    decision = CastDecision(members=kept_members, needs=needs,
                            raw=res.content, alarms=parse_alarms + val_alarms)
    try:
        save_casting(ws, project_id, vol, ch, idx, decision, closed=closed)
    except OSError:
        pass  # 落盘失败不阻断（可写区异常时静默）

    # 缺人需求入队（ADR-022：广播提名 → 工厂生产注册，章前 drain 消化）
    if needs:
        try:
            from .character_factory import CharacterNeed, queue_need
            for n in needs:
                queue_need(ws, project_id, CharacterNeed(
                    role=n.role, description=n.description,
                    realm_hint=n.realm_hint, faction_hint=n.faction_hint,
                    hooks=n.hooks, why_existing_fail=n.why_existing_fail,
                    source="broadcast", vol=vol, ch=ch))
        except Exception:  # noqa: BLE001
            pass
    return decision
