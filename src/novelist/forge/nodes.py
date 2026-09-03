"""构建引擎节点层（docs/10 §7.2 / §7.5 / §4.2 B3）。

F1 最小树三个节点：
- **book**：设定骨架（worldview/characters/style/threads/卷主线一次出齐）→ 写回蓝图 + 落 bible；
- **volume**：单卷主线细化 → `outline/volumes.json`（卷闸门：只展开 vol=1 的 chapter）；
- **chapter**：章节细纲 → `outline/chapters/<vol>-<ch>.md`（front-matter + 行内
  `key_events:`/`出场人物:` 双通道——`parse_key_events`/`parse_cast_decl` 都是行内 regex）。

节点协议（LLM 输出）：`{"artifact": {...}|null, "decide": "done|expand", "reason": "...",
"children": [...], "open_questions": [...]}`。F1 树形由引擎确定性展开（expand/children
仅记录到 reason/日志，arc/beat 层 F4 提供）。

落盘：
- `sync_bible`：蓝图 → bible 文件**确定性全量重写**（剥离 role、补默认字段、主角引用），零 LLM；
- `synthesize_worldstate`：build 末尾**确定性合成** worldstate（time/pending/characters 初始态），
  零 LLM、不进构建树（docs/10 §7.5 末行）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from ..core.llm import LLMMessage, LLMRequest
from ..storage.workspace import Workspace
from .state import Blueprint

KINDS = ("book", "volume", "chapter",
         "worldview", "system", "setting_entry",
         "character_group", "character", "style", "thread_set",
         "arc", "beat")

# 旁支/主干的父子关系（docs/10 §7.1 树形）：父 decide=expand 时 children 细化为该 kind 的子节点
CHILD_KIND: dict[str, str] = {
    "worldview": "system",
    "system": "setting_entry",
    "character_group": "character",
    "volume": "arc",
    "chapter": "beat",
}
# 叶节点（children 不再递归）
LEAF_KINDS = ("style", "thread_set", "arc", "beat", "character", "setting_entry")


# ---- 节点协议 ----
@dataclass
class NodeContext:
    """单节点的全部输入（引擎装配）。"""

    ws: Workspace
    project_id: str
    bp: Blueprint
    provider: Any
    pack: dict
    spec: Any = None  # SeedSpec（book 节点 prompt 用；forge build 重跑时为 None）
    vol: int = 0
    ch: int = 0
    prev_gist: dict | None = None  # 前一章细纲（chapter 节点，因果连续）
    child: dict | None = None      # 父节点 children 中的当前项 brief（旁支/arc/beat 子节点）
    arcs: list[dict] | None = None  # 本卷 arc 产物（chapter 节点 prompt 注入，F4b）
    extra: dict = field(default_factory=dict)  # roll 注入的四块上下文（§7.7）等
    extra_warnings: list[str] = field(default_factory=list)


@dataclass
class NodeResult:
    kind: str
    node_id: str
    ok: bool
    artifact: dict | None = None
    decide: str = "done"
    reason: str = ""
    children: list = field(default_factory=list)  # expand 时的子节点 brief 清单
    warnings: list[str] = field(default_factory=list)
    tokens_in: int = 0   # 本节点 LLM 调用 usage（F5b report 统计；provider 不给则为 0）
    tokens_out: int = 0


# 旁支协议（_protocol_block）的元键：裸产物场景下从顶层剔除，其余键整体当 artifact
_PROTOCOL_META_KEYS = ("decide", "reason", "children", "open_questions")


def _parse_node_reply(raw: str) -> dict:
    """宽松解析节点协议 JSON；结构不对抛 ValueError（引擎重试/回退）。

    兼容两种输出形状（D3 修复，forge/nodes.py）：
    - 带壳：顶层含 artifact（旁支节点 _protocol_block、测试 helper）→ 取 data["artifact"]，
      并期望 decide/reason 齐备（缺 reason 时 run_node 告警）。
    - 裸产物：主干 book/volume/chapter 的 prompt 直接要求顶层业务键 → 剔除协议元键后
      整体当 artifact，无 decide/reason 要求（不告警）。
    """
    text = raw.strip()
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no json object in node reply")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("node reply not an object")
    has_shell = "artifact" in data
    if has_shell:
        art = data.get("artifact")
    else:
        payload = {k: v for k, v in data.items() if k not in _PROTOCOL_META_KEYS}
        art = payload or None
    if art is not None and not isinstance(art, dict):
        raise ValueError("artifact not an object")
    decide = str(data.get("decide") or "done")
    if decide not in ("done", "expand"):
        decide = "done"
    return {
        "artifact": art,
        "decide": decide,
        "reason": str(data.get("reason") or ""),
        "children": data.get("children") or [],
        "open_questions": data.get("open_questions") or [],
        "has_shell": has_shell,
    }


def _dict_of(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


def _str_list(v: Any) -> list[str]:
    return [str(x).strip() for x in (v or []) if str(x).strip()]


# ---- prompt 装配 ----
_BOOK_OUTPUT_PROTOCOL = """{
  "worldview": {"name": "世界名", "power_system": {"mechanic": "核心机制一句话", "levels": ["境界1", "境界2"]},
                "rules": ["世界铁律"], "factions": ["势力"]},
  "characters": [{"id": "char:xxx", "name": "名字", "gender": "male|female|unknown",
                  "role": "protagonist|mentor|rival|love_interest|minor",
                  "core_traits": ["性格1", "性格2"], "background": "背景一句话",
                  "power": {"level": "境界或实力", "faction": "阵营"},
                  "arc": "人物弧线一句话", "first_appear": {"vol": 1, "ch": 1}}],
  "locations": [{"id": "loc:xxx", "name": "地名", "category": "类别", "desc": "描述"}],
  "items": [{"id": "item:xxx", "name": "物品名", "type": "consumable|equipment|artifact|material|currency|other", "desc": "描述"}],
  "style": {"tense": "过去|现在", "narration": "叙事风格说明", "glossary": [{"term": "术语", "note": "解释"}]},
  "threads": [{"id": "pt:xxx", "desc": "伏笔内容", "scope": "book|volume", "target_vol": 1}],
  "volumes": [{"vol": 1, "title": "卷名", "summary": "本卷主线", "key_beats": ["关键转折"],
               "threads_to_payoff": ["pt:xxx"]}],
  "time_origin": "故事时间原点（t=0 锚点）"
}"""


def _book_prompt(ctx: NodeContext) -> tuple[str, str]:
    bp, pack = ctx.bp, ctx.pack
    meta = bp.get("meta") or {}
    scale = meta.get("scale") or {}
    spec = ctx.spec
    proto = _dict_of(_dict_of(spec).get("protagonist_hint")) if spec is not None else {}
    proto_block = (f"主角线索：name={proto.get('name') or '（未给）'}，"
                   f"gender={proto.get('gender') or 'unknown'}，cheat={proto.get('cheat') or '—'}") \
        if spec is not None else "（forge build 重跑：蓝图已有内容为准）"
    pkg = pack or {}
    pkg_block = (
        f"流派模板（{meta.get('template') or '通用'}）：\n"
        f"  境界体系: {'-'.join((pkg.get('power_system') or {}).get('levels') or []) or '（无）'}\n"
        f"  力量机制: {(pkg.get('power_system') or {}).get('mechanic') or '（无）'}\n"
        f"  角色阵容: {json.dumps(pkg.get('character_slots') or [], ensure_ascii=False)}\n"
        f"  卷弧提示: {pkg.get('volume_arc_hint') or '（无）'}\n"
        f"  默认文风: {json.dumps((pkg.get('default_style') or {}).get('tone') or [], ensure_ascii=False)}\n"
        f"  禁用词: {json.dumps(pkg.get('default_banned') or [], ensure_ascii=False)}"
    )
    wv = bp.get("worldview") or {}
    ps = _dict_of(wv.get("power_system"))
    fixed = {
        "title": meta.get("title"),
        "genre": meta.get("genre"),
        "logline": meta.get("logline"),
        "scale": scale,
        "mechanic": ps.get("mechanic"),
        "levels": ps.get("levels"),
        "style.tone": bp.get("style.tone"),
        "style.pov": bp.get("style.pov"),
        "protagonist": proto_block,
    }
    user = f"""你是网文立项设定师。为一部新书产出完整设定骨架。只输出 JSON，不要任何解释。

【创意提炼 / 已确定项（不得改动，须原样保留）】
{json.dumps(fixed, ensure_ascii=False, indent=2)}

{pkg_block}

【要求】
1. 主角必给一张卡（role=protagonist）；原话没给主角名就按卖点拟一个合理的。
2. volumes 恰好 {scale.get('volumes', 3)} 卷（vol=1..N），每卷给 title/summary/key_beats/threads_to_payoff。
3. worldview.power_system.levels 沿用模板境界体系，可按本书微调。
4. 所有 id 用前缀：char:/loc:/item:/pt:。
5. key_beats 每卷 3–6 条；threads 给 3–8 条主线伏笔。
6. 世界观 rules（世界铁律）2–5 条，必须与力量机制自洽。
7. hidden_level 只给"表面修为与实际战力不符"的人物（扮猪吃虎型主角等）；普通人卡省略该键。

按以下 JSON 输出（键名严格一致，缺省用空对象/空数组）：
{_BOOK_OUTPUT_PROTOCOL}"""
    return "你是网文立项设定师（Forge book 节点，docs/10 §7.2）。", user


def _volume_prompt(ctx: NodeContext) -> tuple[str, str]:
    bp = ctx.bp
    meta = bp.get("meta") or {}
    scale = meta.get("scale") or {}
    vol = ctx.vol
    plan = next((v for v in bp.section("volumes") if v.get("vol") == vol), None)
    plan_block = json.dumps(plan, ensure_ascii=False, indent=2) if plan else "（无 book 规划，自行拟定）"
    arc = (ctx.pack or {}).get("volume_arc_hint") or "（无模板卷弧提示）"
    user = f"""你是卷大纲师。为第 {vol} 卷写出主线（全书 {scale.get('volumes', '?')} 卷 × {scale.get('chapters_per_volume', '?')} 章）。

【本卷规划（book 产物）】{plan_block}

【流派卷弧提示】{arc}

【输出 JSON】
{{
  "vol": {vol},
  "title": "卷名",
  "summary": "本卷主线一句话（每章生成时注入）",
  "key_beats": ["3-6 个关键转折"],
  "threads_to_payoff": ["本卷须回收的伏笔 id，没有则空数组"],
  "chapter_notes": "本卷各章走向提示（供细纲师），两三句话"
}}
只输出 JSON。"""
    rc = ctx.extra.get("roll_context")  # forge roll：§7.7 四块上下文
    if rc:
        user += f"\n\n【滚动上下文（前卷事实，优先于规划）】\n{rc}\n以上为第 {vol - 1} 卷已发生的实然，本卷细纲必须承接。"
    return f"你是卷大纲师（Forge volume 节点，第 {vol} 卷，docs/10 §7.2）。", user


def _chapter_prompt(ctx: NodeContext) -> tuple[str, str]:
    bp = ctx.bp
    meta = bp.get("meta") or {}
    scale = meta.get("scale") or {}
    vol, ch = ctx.vol, ctx.ch
    volume = next((v for v in bp.section("volumes") if v.get("vol") == vol), None) or {}
    vol_block = json.dumps({k: volume.get(k) for k in ("title", "summary", "key_beats", "chapter_notes") if volume.get(k)},
                           ensure_ascii=False, indent=2) or "（无卷主线）"
    prev = ctx.prev_gist
    prev_block = "（第一章，无前一章）"
    if prev:
        prev_block = json.dumps({k: prev.get(k) for k in ("title", "key_events", "turns", "characters") if prev.get(k)},
                                ensure_ascii=False, indent=2)
    actual_block = ""
    _actual = _actual_events_block(ctx)
    if _actual:
        actual_block = "\n\n【已落定实情（memory 实然，非计划态；细纲不得与它冲突）】\n" + _actual
    cards = bp.section("characters")
    char_block = json.dumps([
        {k: c.get(k) for k in ("id", "name", "role", "gender", "core_traits", "power") if c.get(k)}
        for c in cards
    ], ensure_ascii=False, indent=2) or "（无角色卡——book 节点应先产出）"
    rhythm = (ctx.pack or {}).get("chapter_rhythm") or ["opening-hook", "setup", "conflict", "climax", "cliffhanger"]
    # F4b：本卷 arc 产物（若有）→ 按章序均匀分配，注入当前章所属弧
    arc_line = "（无章段弧）"
    cur_arc = None
    if ctx.arcs:
        K = int((bp.get("meta.scale") or {}).get("chapters_per_volume", 20)) or 1
        # 均匀分配：第 ch 章属于 arcs[(ch-1)*len(arcs)//K]
        cur_arc = ctx.arcs[(ch - 1) * len(ctx.arcs) // K] if ch <= K else ctx.arcs[-1]
        arc_line = json.dumps(cur_arc, ensure_ascii=False)
    user = f"""你是细纲师。写第 {vol} 卷第 {ch} 章的章节细纲（全书 {scale.get('chapters_per_volume', '?')} 章/卷）。

【本卷主线】{vol_block}

【本章所属章段弧】{arc_line}

【前一章（因果连续，必须衔接）】
{prev_block}
{actual_block}
【本章可用角色卡】
{char_block}

【节奏提示】{json.dumps(rhythm, ensure_ascii=False)}

【纪律】
1. key_events 恰好 2–3 个（每章事件数上限），每个一句话、可执行、含动作与结果。
2. after_days：相对上一事件的天数（连续推进填 0；有明确间隔填天数，如 3）。
3. characters 用角色 id（char:xxx），只列本章实际出场者。
4. threads_involved 用伏笔 id（pt:xxx）；本章没碰伏笔就空数组。
5. turns 1–3 条：本章转折/推进点。

【输出 JSON】
{{
  "title": "本章标题（不带'第 N 章'）",
  "pov": "视角（默认：第三人称限知（主角视角））",
  "key_events": ["事件1", "事件2"],
  "turns": ["转折/推进1"],
  "characters": ["char:xxx"],
  "threads_involved": ["pt:xxx"],
  "after_days": 0
}}
只输出 JSON。"""
    return f"你是章节细纲师（Forge chapter 节点，第 {vol} 卷第 {ch} 章，docs/10 §7.2）。", user


# ---- F4 旁支节点 prompt（docs/10 §7.1 树形 + §7.2 判据引导）----
def _actual_events_block(ctx: NodeContext) -> str:
    """C2/ADR-023：前情补"已落定实情"（memory 实然，非计划态 gist）。

    章细纲默认只喂前一章**计划态**（prev_gist）；正文写偏后（如标题/事件没按细纲走），
    下一章细纲感知不到 → 细纲与落盘正文漂移。这里在计划态之外追加按时间序的
    `plot_events` 实然查询块（forge 纯细纲期无正文记忆时返回空，不改变原行为）。
    策划低频决策用确定性查询；正文高频注入仍走紧凑前情（ADR-023 D1 补充）。
    """
    try:
        from novelist.core.memory import query_recent_actual_events

        evs = query_recent_actual_events(ctx.ws, ctx.project_id, limit=6)
    except Exception:  # noqa: BLE001 - 实然块失败退回计划态前情，不阻断细纲
        return ""
    if not evs:
        return ""
    lines = []
    for e in evs:
        who = f"（{'、'.join(e['participants'][:4])}）" if e.get("participants") else ""
        lines.append(f"- v{e['vol']}c{e['ch']} {e['summary']}{who}")
    return "\n".join(lines)


def _protocol_block(fields: str) -> str:
    return f"""【输出 JSON】
{{
{fields}
  "decide": "done 或 expand",
  "reason": "为什么这么判（必填）",
  "children": [{{"id": "子节点标识", "brief": "子节点做什么", "focus": "细化重点"}}]
}}
- decide=done：本层定稿；decide=expand：需要进一步细化，children 给出子节点清单（≤4 个）。
- 只输出 JSON。"""


def _sibling_block(bp: Blueprint) -> str:
    """已定稿的兄弟节点摘要（旁支互相引用，避免冲突）。"""
    wv = bp.get("worldview") or {}
    ps = _dict_of(wv.get("power_system"))
    cards = [{"id": c.get("id"), "name": c.get("name"), "role": c.get("role"),
              "power": _dict_of(c.get("power")).get("level")} for c in bp.section("characters")]
    block = {
        "worldview": {"name": wv.get("name"), "mechanic": ps.get("mechanic"),
                      "levels": ps.get("levels"), "rules": wv.get("rules"),
                      "factions": wv.get("factions")},
        "characters": cards,
        "style": {k: bp.get(f"style.{k}") for k in ("pov", "tense", "narration", "tone")},
        "threads": [{"id": t.get("id"), "desc": t.get("desc"), "scope": t.get("scope"),
                     "target_vol": t.get("target_vol")} for t in bp.section("threads")],
    }
    return json.dumps(block, ensure_ascii=False, indent=2)


def _worldview_prompt(ctx: NodeContext) -> tuple[str, str]:
    bp = ctx.bp
    wv = bp.get("worldview") or {}
    ps = _dict_of(wv.get("power_system"))
    user = f"""你是世界观构建师。细化本书世界观（骨架已有，做深化而非推倒重来）。

【现有骨架】
{json.dumps({"name": wv.get("name"), "mechanic": ps.get("mechanic"), "levels": ps.get("levels"),
             "rules": wv.get("rules"), "factions": wv.get("factions")}, ensure_ascii=False, indent=2)}

【判据引导】体系有 ≥2 个子维度（等级+派系+资源/地理）→ 建议 expand（children=各子维度 system）；
单一线性等级且规则自洽 → done。

【要求】artifact 可为 null（只做分解）或完整 worldview 细化（name/power_system/rules/factions/civilizations）；
不得改动现有 power_system.levels（已与主角金手指对齐）；rules 每条一句话、可执行、相互不矛盾。"""
    return "你是世界观构建师（Forge worldview 节点，docs/10 §7.1 L1）。", user


def _system_prompt(ctx: NodeContext) -> tuple[str, str]:
    child = ctx.child or {}
    wv = ctx.bp.get("worldview") or {}
    user = f"""你是体系设定师。细化世界观的其中一个子维度。

【父节点给的本维度任务】{json.dumps(child, ensure_ascii=False)}
【世界骨架】mechanic={_dict_of(wv.get('power_system')).get('mechanic')}

【要求】artifact = 本维度的完整设定（title/kind/desc），并给出可入库的设定条目
settings（id 用 set: 前缀，每条含 keywords/text，text 一段话，知识库检索用）。

{_protocol_block('  "artifact": {"title": "维度名", "kind": "power|faction|geo|resource", "desc": "一段话"},\n  "settings": [{"id": "set:xxx", "keywords": ["词1"], "text": "设定正文"}],')}"""
    return "你是体系设定师（Forge system 节点，docs/10 §7.1 L2）。", user


def _setting_entry_prompt(ctx: NodeContext) -> tuple[str, str]:
    child = ctx.child or {}
    user = f"""你是设定条目撰写者。把下面的设定要点写成一条可入库的设定条目。

【要点】{json.dumps(child, ensure_ascii=False)}

{_protocol_block('  "artifact": {"id": "set:xxx", "keywords": ["词1", "词2"], "text": "设定正文（一段话，含数值/边界等硬细节）"},')}"""
    return "你是设定条目撰写者（Forge setting_entry 节点，docs/10 §7.1 L3）。", user


def _character_group_prompt(ctx: NodeContext) -> tuple[str, str]:
    bp = ctx.bp
    scale = bp.get("meta.scale") or {}
    cards = [{"id": c.get("id"), "name": c.get("name"), "role": c.get("role"),
              "core_traits": c.get("core_traits"), "background": c.get("background")}
             for c in bp.section("characters")]
    pkg = ctx.pack or {}
    user = f"""你是角色阵容规划师。审视现有角色阵容，规划谁需要完整人物卡。

【现有角色骨架】
{json.dumps(cards, ensure_ascii=False, indent=2)}

【流派阵容模板】{json.dumps(pkg.get('character_slots') or [], ensure_ascii=False)}

【判据引导】主线人物（protagonist/mentor/rival/love_interest）→ 建议 expand
（children=各人物，骨架→完整卡）；阶段配角 → done（JIT 期再补）。
全书规模 {scale.get('volumes', '?')} 卷 × {scale.get('chapters_per_volume', '?')} 章。

【要求】artifact 可为 null 或阵容调整说明；children 每项 {{"id": "char:xxx", "brief": "该人物需要补什么", "focus": "弧线/关系/秘密"}}。"""
    return "你是角色阵容规划师（Forge character_group 节点，docs/10 §7.1 L1）。", user


def _character_prompt(ctx: NodeContext) -> tuple[str, str]:
    child = ctx.child or {}
    cid = str(child.get("id") or "")
    card = ctx.bp.find_by_id("characters", cid) if cid else None
    block = json.dumps(card, ensure_ascii=False, indent=2) if card else json.dumps(child, ensure_ascii=False, indent=2)
    user = f"""你是人物卡撰写者。把下面的角色骨架细化为完整人物卡（做深化，不改 id/name/role）。

【现有骨架】
{block}

【要求】artifact = 完整人物卡：在骨架字段之外补 aliases/background（三句内，含动机）/
power（level+faction）/arc（人物弧线一句话）/first_appear{{vol,ch}}/relationships[]
（[{{"target": "char:xxx", "type": "关系", "note": "一句话"}}]，target 必须是已有角色 id）/
secret（人物秘密，可空字符串）。id 用原骨架的 id。

{_protocol_block('  "artifact": { …完整人物卡… },')}"""
    return "你是人物卡撰写者（Forge character 节点，docs/10 §7.1 L2）。", user


def _style_prompt(ctx: NodeContext) -> tuple[str, str]:
    bp = ctx.bp
    st = bp.get("style") or {}
    pkg = ctx.pack or {}
    user = f"""你是文风定稿师。细化本书文风（现有骨架做深化）。

【现有骨架】{json.dumps(st, ensure_ascii=False)}
【流派默认】{json.dumps(pkg.get('default_style') or {}, ensure_ascii=False)}，
禁用词 {json.dumps(pkg.get('default_banned') or [], ensure_ascii=False)}

【要求】artifact = 完整文风：pov/tense/narration/tone[]/target_words_per_chapter/
forbidden_words[]（禁用词，含现代词与陈词滥调）/glossary[{{term,note}}]（本书术语表）。
不改 pov/tense（已与商讨答案对齐）。

{_protocol_block('  "artifact": { …完整文风… },')}"""
    return "你是文风定稿师（Forge style 节点，docs/10 §7.1 L1）。", user


def _thread_set_prompt(ctx: NodeContext) -> tuple[str, str]:
    bp = ctx.bp
    scale = bp.get("meta.scale") or {}
    threads = [{"id": t.get("id"), "desc": t.get("desc"), "scope": t.get("scope"),
                "target_vol": t.get("target_vol"), "status": t.get("status")}
               for t in bp.section("threads")]
    user = f"""你是伏笔布局师。审视全书伏笔布局并细化（book 骨架的 threads 做深化）。

【现有伏笔】{json.dumps(threads, ensure_ascii=False, indent=2)}
【全书规模】{scale.get('volumes', '?')} 卷；各卷须回收伏笔见 blueprint volumes.threads_to_payoff。

【要求】artifact = {{"threads": [...]}}：补全每条伏笔的 plant_vol/plant_desc（埋设卷与方式）/
payoff_desc（回收方式一句话）；scope=volume 的必须有 target_vol；可新增 1–3 条卷级小伏笔（id pt:xxx）。
伏笔要能被章节细纲的 threads_involved 引用。

{_protocol_block('  "artifact": {"threads": [ …伏笔清单… ]},')}"""
    return "你是伏笔布局师（Forge thread_set 节点，docs/10 §7.1 L1）。", user


def _arc_prompt(ctx: NodeContext) -> tuple[str, str]:
    child = ctx.child or {}
    vol = ctx.vol
    plan = next((v for v in ctx.bp.section("volumes") if v.get("vol") == vol), None) or {}
    user = f"""你是章段弧线师。把第 {vol} 卷内的一个章段任务细化为 3–5 章的小弧。

【卷主线】{json.dumps({k: plan.get(k) for k in ("title", "summary", "key_beats") if plan.get(k)}, ensure_ascii=False)}
【本弧任务】{json.dumps(child, ensure_ascii=False)}

【要求】artifact = {{"title": "弧名", "brief": "弧线一句话", "focus": "冲突/势力/场景重点",
"chapters_hint": "建议覆盖的章数与节奏，如'第 3–5 章：铺垫→交锋→反转'"}}。

{_protocol_block('  "artifact": { …本弧产物… },')}"""
    return "你是章段弧线师（Forge arc 节点，docs/10 §7.1 L2）。", user


def _beat_prompt(ctx: NodeContext) -> tuple[str, str]:
    child = ctx.child or {}
    vol, ch = ctx.vol, ctx.ch
    gist = next((g for g in ctx.bp.section("chapters")
                 if g.get("vol") == vol and g.get("ch") == ch), None) or {}
    user = f"""你是重场戏节拍师。为第 {vol} 卷第 {ch} 章（重场戏）写"拍"级节拍提示。

【本章细纲】{json.dumps({k: gist.get(k) for k in ("title", "key_events", "turns") if gist.get(k)}, ensure_ascii=False)}
【节拍任务】{json.dumps(child, ensure_ascii=False)}

【要求】artifact = {{"beats": ["拍1：场景/动作/情绪一句话", "拍2：…", …]}}；4–8 拍，
按叙事顺序，每拍一句话，关键拍标注情绪强度（如"高"）。

{_protocol_block('  "artifact": {"beats": ["…"]},')}"""
    return "你是重场戏节拍师（Forge beat 节点，docs/10 §7.1 L4）。", user


# ---- apply（落盘 + 蓝图回写，provenance 保护）----
def _merge_worldview(bp: Blueprint, art_wv: dict) -> None:
    """worldview 合并（保护 user 字段；power_system 二级路径逐键保护）。"""
    wv = dict(bp.get("worldview") or {})
    for k in ("name", "power_system", "rules", "factions", "civilizations"):
        v = _dict_of(art_wv).get(k)
        if v in (None, ""):
            continue
        if isinstance(v, list) and not v:
            continue
        if k == "power_system":
            cur_ps = dict(wv.get("power_system") or {})
            for pk in ("mechanic", "levels"):
                pv = v.get(pk)
                if pv in (None, ""):
                    continue
                if isinstance(pv, list) and not pv:
                    continue
                if bp.is_protected(f"worldview.power_system.{pk}"):
                    continue
                cur_ps[pk] = pv
                bp.set_provenance(f"worldview.power_system.{pk}", "llm", 0.8)
            wv["power_system"] = cur_ps
            continue
        if bp.is_protected(f"worldview.{k}"):
            continue
        wv[k] = v
        bp.set_provenance(f"worldview.{k}", "llm", 0.8)
    bp.data["worldview"] = wv


def _merge_style(bp: Blueprint, style_art: dict) -> None:
    """style 合并（保护 user 字段；glossary 按 term 去重合并）。"""
    st = dict(bp.get("style") or {})
    for k in ("pov", "tense", "narration", "tone", "target_words_per_chapter", "forbidden_words"):
        v = _dict_of(style_art).get(k)
        if v in (None, ""):
            continue
        if isinstance(v, list) and not v:
            continue
        if bp.is_protected(f"style.{k}"):
            continue
        st[k] = v
        bp.set_provenance(f"style.{k}", "llm", 0.8)
    gloss = _dict_of(style_art).get("glossary")
    if isinstance(gloss, list) and gloss:
        existing = [g for g in (st.get("glossary") or []) if isinstance(g, dict)]
        seen = {g.get("term") for g in existing}
        for g in gloss:
            if isinstance(g, dict) and g.get("term") and g["term"] not in seen:
                existing.append(g)
                seen.add(g["term"])
        st["glossary"] = existing
        bp.set_provenance("style.glossary", "llm", 0.8)
    bp.data["style"] = st


# items.schema `type` 枚举 + 中文类别关键词映射（D3：通用包 LLM/ingest 只给中文
# category/kind，schema 却必填英文枚举 type 且 additionalProperties=false）。
_ITEM_TYPE_ENUM = ("consumable", "equipment", "artifact", "material", "currency", "other")

# 顺序即优先级：specific（法宝/丹药/货币）在前，宽泛词表（武器防具材质）殿后。
_ITEM_TYPE_RULES: list[tuple[tuple[str, ...], str]] = [
    (("法器", "法宝", "灵器", "宝器", "仙器", "神器", "古宝", "灵宝", "artifact"), "artifact"),
    (("丹", "药", "剂", "灵液", "灵乳", "圣水", "散", "膏", "consumable"), "consumable"),
    (("灵石", "金币", "银两", "铜钱", "钱", "币", "钻石", "currency"), "currency"),
    (("材料", "矿", "草", "木", "皮", "骨", "妖丹", "兽核", "精血", "material"), "material"),
    (("剑", "刀", "枪", "棍", "弓", "杖", "扇", "鼎", "镜", "甲", "盔", "靴", "袍",
      "戒指", "手环", "项链", "equipment"), "equipment"),
]


def _normalize_item_type(entry: dict) -> str:
    """条目 → items.schema 合法 type：显式英文枚举直取，中文类别按词表映射，兜底 other。"""
    for v in (entry.get("type"), entry.get("category"), entry.get("kind")):
        if isinstance(v, str) and v.strip().lower() in _ITEM_TYPE_ENUM:
            return v.strip().lower()
    haystack = " ".join(str(entry.get(k) or "") for k in ("type", "category", "kind", "name"))
    for keys, t in _ITEM_TYPE_RULES:
        if any(k in haystack for k in keys):
            return t
    return "other"


def _apply_items(bp: Blueprint, section: str, items: list, *, default_role: str = "minor") -> None:
    """数组段按 id upsert；已有条目跳过 user 保护字段。"""
    for item in items or []:
        if not isinstance(item, dict) or not item.get("id") or not item.get("name"):
            continue
        iid = item["id"]
        item.setdefault("role", default_role)
        existing = bp.find_by_id(section, iid)
        if existing is None:
            bp.upsert(section, dict(item))
            bp.set_provenance(f"{section}[{iid}]", "llm", 0.8)
            continue
        merged = dict(existing)
        for k, v in item.items():
            if bp.is_protected(f"{section}[{iid}].{k}"):
                continue
            merged[k] = v
            bp.set_provenance(f"{section}[{iid}].{k}", "llm", 0.8)
        bp.upsert(section, merged)


def _upsert_volume_bp(bp: Blueprint, item: dict) -> None:
    """蓝图 volumes 按 vol upsert（volume 条目无 id，vol 即唯一键）。"""
    vol = int(item["vol"])
    vols = bp.section("volumes")
    for i, x in enumerate(vols):
        if x.get("vol") == vol:
            merged = dict(x)
            for k, v in item.items():
                if k == "vol":
                    continue
                if bp.is_protected(f"volumes[{vol}].{k}"):
                    continue
                merged[k] = v
            vols[i] = merged
            return
    vols.append(item)


def _apply_book(ctx: NodeContext, node: dict) -> list[str]:
    bp, art = ctx.bp, node["artifact"] or {}
    warns: list[str] = []

    # worldview：合并（保护 user 字段）
    if art.get("worldview"):
        _merge_worldview(bp, _dict_of(art.get("worldview")))

    # characters（含主角兜底）
    _apply_items(bp, "characters", art.get("characters") or [])
    if not any(c.get("role") == "protagonist" for c in bp.section("characters")):
        title = (bp.get("meta") or {}).get("title") or "主角"
        bp.upsert("characters", {"id": "char:protagonist", "name": title, "gender": "unknown",
                                 "role": "protagonist", "core_traits": [], "status": "active"})
        bp.set_provenance("characters[char:protagonist].name", "llm", 0.6)
        warns.append("模型未给主角，兜底建档 char:protagonist")

    # locations / items（name 必填的段）
    for section in ("locations", "items"):
        _apply_items(bp, section, art.get(section) or [])
    # skills / settings（无 name 约束的段，仅无 id 冲突时 upsert）
    for section, has_name in (("skills", True), ("settings", False)):
        for item in art.get(section) or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            if has_name and not item.get("name"):
                continue
            existing = bp.find_by_id(section, item["id"])
            if existing is None:
                bp.upsert(section, dict(item))
                bp.set_provenance(f"{section}[{item['id']}]", "llm", 0.8)

    # threads
    _apply_threads(bp, art.get("threads") or [])

    # style（保护 user 字段）
    if art.get("style"):
        _merge_style(bp, _dict_of(art.get("style")))

    # time_origin
    if art.get("time_origin") and not bp.is_protected("meta.time_origin"):
        bp.data["meta"]["time_origin"] = art["time_origin"]
        bp.set_provenance("meta.time_origin", "llm", 0.8)

    # volumes（补齐 chapter_range，按 vol upsert）
    scale = bp.get("meta.scale") or {}
    K = int(scale.get("chapters_per_volume", 20))
    for v in art.get("volumes") or []:
        if not isinstance(v, dict) or not v.get("vol"):
            continue
        v.setdefault("title", f"第 {v['vol']} 卷")
        v.setdefault("summary", "")
        v.setdefault("key_beats", [])
        v.setdefault("threads_to_payoff", [])
        vol_n = int(v["vol"])
        v.setdefault("chapter_range", [(vol_n - 1) * K + 1, vol_n * K])
        _upsert_volume_bp(bp, v)
    return warns


def _apply_threads(bp: Blueprint, threads: list) -> None:
    for t in threads or []:
        if not isinstance(t, dict) or not t.get("id") or not t.get("desc"):
            continue
        tid = t["id"]
        t.setdefault("scope", "book")
        t.setdefault("status", "unplanned")
        existing = bp.find_by_id("threads", tid)
        if existing is None:
            bp.upsert("threads", dict(t))
            bp.set_provenance(f"threads[{tid}]", "llm", 0.8)
            continue
        merged = dict(existing)
        for k, v in t.items():
            if bp.is_protected(f"threads[{tid}].{k}"):
                continue
            merged[k] = v
            bp.set_provenance(f"threads[{tid}].{k}", "llm", 0.8)
        bp.upsert("threads", merged)


def _load_json_list(ws: Workspace, project_id: str, rel: str) -> list:
    p = ws._abs(f"{project_id}/{rel}")  # noqa: SLF001
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def chapter_range_of(vol: int, chapters_per_volume: int) -> list[int]:
    start = (vol - 1) * chapters_per_volume + 1
    return [start, start + chapters_per_volume - 1]


def _apply_volume(ctx: NodeContext, node: dict) -> list[str]:
    art = node["artifact"] or {}
    bp = ctx.bp
    scale = bp.get("meta.scale") or {}
    K = int(scale.get("chapters_per_volume", 20))
    vol = ctx.vol
    row = {
        "id": f"vol:{vol}",
        "vol": vol,
        "order": vol,
        "title": str(art.get("title") or f"第 {vol} 卷"),
        "summary": str(art.get("summary") or ""),
        "chapter_range": chapter_range_of(vol, K),
        "key_beats": _str_list(art.get("key_beats")),
        "threads_to_payoff": _str_list(art.get("threads_to_payoff")),
        "target_words": int(scale.get("target_words_per_chapter", 2400)) * K,
    }
    if art.get("chapter_notes"):
        row["chapter_notes"] = str(art["chapter_notes"])
    # outline/volumes.json upsert by vol
    existing = _load_json_list(ctx.ws, ctx.project_id, "outline/volumes.json")
    replaced = False
    for i, x in enumerate(existing):
        if x.get("vol") == vol:
            existing[i] = row
            replaced = True
            break
    if not replaced:
        existing.append(row)
    existing.sort(key=lambda x: x.get("vol", 0))
    ctx.ws.write_json(ctx.ws.outline_volumes_path(ctx.project_id), existing)
    _upsert_volume_bp(bp, row)
    return []


def _upsert_chapter_bp(bp: Blueprint, gist: dict) -> None:
    chapters = bp.section("chapters")
    vol, ch = gist["vol"], gist["ch"]
    for i, x in enumerate(chapters):
        if x.get("vol") == vol and x.get("ch") == ch:
            chapters[i] = gist
            return
    chapters.append(gist)


def render_gist_md(gist: dict, vol: int, ch: int, char_names: list[str]) -> str:
    """细纲 md：front-matter JSON + 行内 key_events/出场人物（双通道兼容）。

    - `parse_gist`（core/bible.py）优先读 front-matter（结构化）；
    - `parse_key_events` / `parse_cast_decl`（orchestrator）读行内 `key_events:` / `出场人物:`。
    """
    fm = {
        "id": f"ch:{vol}:{ch}",
        "vol": vol,
        "ch": ch,
        "title": str(gist.get("title") or f"第 {ch} 章"),
        "pov": str(gist.get("pov") or ""),
        "key_events": [str(e) for e in (gist.get("key_events") or [])],
        "turns": [str(t) for t in (gist.get("turns") or [])],
        "characters": [str(c) for c in (gist.get("characters") or [])],
        "threads_involved": [str(t) for t in (gist.get("threads_involved") or [])],
        "after_days": int(gist.get("after_days") or 0),
        "beats": [str(b) for b in (gist.get("beats") or [])],  # F4b：重场戏拍级提示
    }
    parts = [
        "---",
        json.dumps(fm, ensure_ascii=False),
        "---",
        "",
        # ADR-020 决策一（延迟拟题）：行内不写标题文字——本 md 全篇会作为
        # gist_text_for_events 注入正文 prompt，标题留在 front-matter（给人看 +
        # parse_gist 结构化读取），正文生成期一律看不见，避免模型在事件开头复写标题。
        f"# 第 {ch} 章",
        "",
        f"key_events: {json.dumps(fm['key_events'], ensure_ascii=False)}",
        "",
        f"出场人物: {json.dumps(char_names, ensure_ascii=False)}",
        "",
        "细纲要点：",
        f"- 事件：{'；'.join(fm['key_events']) or '（模型未给）'}",
    ]
    if fm["turns"]:
        parts.append(f"- 转折：{'；'.join(fm['turns'])}")
    if fm["threads_involved"]:
        parts.append(f"- 伏笔：{'、'.join(fm['threads_involved'])}")
    if fm["beats"]:
        parts.append("- 节拍：" + " ｜ ".join(fm["beats"]))
    return "\n".join(parts) + "\n"


def _apply_chapter(ctx: NodeContext, node: dict) -> list[str]:
    art = node["artifact"] or {}
    bp = ctx.bp
    vol, ch = ctx.vol, ctx.ch
    warns: list[str] = []

    # characters：id 优先，名字回退映射；悬空引用丢弃
    cids: list[str] = []
    names: list[str] = []
    for raw in art.get("characters") or []:
        key = str(raw)
        card = bp.find_by_id("characters", key)
        if card is None:
            card = next((x for x in bp.section("characters") if x.get("name") == key), None)
            if card is not None:
                key = card["id"]
        if card is None:
            warns.append(f"chapter {vol}-{ch}: 悬空角色引用丢弃 {key!r}")
            continue
        cids.append(key)
        names.append(card.get("name") or key)
    threads = [str(t) for t in (art.get("threads_involved") or [])
               if str(t).startswith(("pt:", "thread:"))]
    key_events = _str_list(art.get("key_events"))[:6]
    if not key_events:
        if not art:
            warns.append(f"chapter {vol}-{ch}: 模型产物为空（裸 JSON 未含业务键或空回复），"
                         f"落盘占位细纲——请检查上游 prompt/解析")
        else:
            warns.append(f"chapter {vol}-{ch}: key_events 为空，用占位事件兜底")
        key_events = [f"第 {ch} 章主线推进"]
    gist = {
        "vol": vol,
        "ch": ch,
        "title": str(art.get("title") or f"第 {ch} 章"),
        "pov": str(art.get("pov") or "") or "第三人称限知（主角视角）",
        "key_events": key_events,
        "turns": _str_list(art.get("turns"))[:6],
        "characters": cids,
        "threads_involved": threads,
        "after_days": int(art.get("after_days") or 0),
    }
    md = render_gist_md(gist, vol, ch, names)
    p = ctx.ws.outline_chapter_path(ctx.project_id, vol, ch)
    p.parent.mkdir(parents=True, exist_ok=True)
    ctx.ws.write_text(p, md)
    _upsert_chapter_bp(bp, gist)
    return warns


# ---- F4 旁支/arc/beat apply ----
def _upsert_settings(bp: Blueprint, items: list) -> None:
    """settings 段按 id upsert（set:* 条目，知识库检索用）。"""
    for item in items or []:
        if not isinstance(item, dict) or not item.get("id") or not item.get("text"):
            continue
        iid = str(item["id"])
        if not iid.startswith("set:"):
            iid = f"set:{iid}"
        row = dict(item)
        row["id"] = iid
        row.setdefault("keywords", [])
        existing = bp.find_by_id("settings", iid)
        if existing is None:
            bp.upsert("settings", row)
            bp.set_provenance(f"settings[{iid}]", "llm", 0.8)
            continue
        merged = dict(existing)
        for k, v in row.items():
            if bp.is_protected(f"settings[{iid}].{k}"):
                continue
            merged[k] = v
            bp.set_provenance(f"settings[{iid}].{k}", "llm", 0.8)
        bp.upsert("settings", merged)


def _apply_worldview(ctx: NodeContext, node: dict) -> list[str]:
    art = node["artifact"]
    if art:
        _merge_worldview(ctx.bp, art)
    return []


def _apply_system(ctx: NodeContext, node: dict) -> list[str]:
    art = _dict_of(node["artifact"])
    _upsert_settings(ctx.bp, art.get("settings") or [])
    return []


def _apply_setting_entry(ctx: NodeContext, node: dict) -> list[str]:
    art = _dict_of(node["artifact"])
    if art.get("text"):
        _upsert_settings(ctx.bp, [art])
    return []


def _apply_character_group(ctx: NodeContext, node: dict) -> list[str]:
    art = _dict_of(node["artifact"])
    if art.get("characters"):
        _apply_items(ctx.bp, "characters", art["characters"])
    return []


def _apply_character(ctx: NodeContext, node: dict) -> list[str]:
    """骨架 → 完整卡：id upsert（不改 id/name/role 已由 _apply_items 的保护语义保证）。"""
    art = _dict_of(node["artifact"])
    if art.get("id") and art.get("name"):
        _apply_items(ctx.bp, "characters", [art])
    return []


def _apply_style(ctx: NodeContext, node: dict) -> list[str]:
    art = _dict_of(node["artifact"])
    if art:
        _merge_style(ctx.bp, art)
    return []


def _apply_thread_set(ctx: NodeContext, node: dict) -> list[str]:
    art = _dict_of(node["artifact"])
    _apply_threads(ctx.bp, art.get("threads") or [])
    return []


def _load_arcs(ws: Workspace, project_id: str) -> list[dict]:
    return _load_json_list(ws, project_id, "outline/arcs.json")


def _apply_arc(ctx: NodeContext, node: dict) -> list[str]:
    """章段小弧 → outline/arcs.json upsert（自定决策：arcs 独立文件，不动 volumes.json schema）。"""
    art = _dict_of(node["artifact"])
    vol = ctx.vol
    child = ctx.child or {}
    aid = str(art.get("id") or child.get("id") or f"arc:{vol}:{len(_load_arcs(ctx.ws, ctx.project_id)) + 1}")
    row = {
        "id": aid if aid.startswith("arc:") else f"arc:{aid}",
        "vol": vol,
        "title": str(art.get("title") or child.get("brief") or f"第 {vol} 卷章段"),
        "brief": str(art.get("brief") or child.get("brief") or ""),
        "focus": str(art.get("focus") or child.get("focus") or ""),
        "chapters_hint": str(art.get("chapters_hint") or ""),
    }
    arcs = [x for x in _load_arcs(ctx.ws, ctx.project_id) if x.get("id") != row["id"]]
    arcs.append(row)
    arcs.sort(key=lambda x: (int(x.get("vol") or 0), str(x.get("id") or "")))
    ctx.ws.write_json(ctx.ws._abs(f"{ctx.project_id}/outline/arcs.json"), arcs)  # noqa: SLF001
    return []


def _apply_beat(ctx: NodeContext, node: dict) -> list[str]:
    """拍级提示并入章细纲：gist.beats + 细纲 md front-matter（自定决策：beat 属章，不单独建文件）。"""
    art = _dict_of(node["artifact"])
    beats = _str_list(art.get("beats"))[:12]
    if not beats:
        return []
    vol, ch = ctx.vol, ctx.ch
    bp = ctx.bp
    gist = next((g for g in bp.section("chapters") if g.get("vol") == vol and g.get("ch") == ch), None)
    if gist is None:
        return [f"beat {vol}-{ch}: 未找到章细纲，节拍丢弃"]
    gist["beats"] = beats
    bp.set_provenance(f"chapters[{vol}-{ch}].beats", "llm", 0.8)
    # 重写细纲 md（front-matter 带 beats；行内通道不变）
    cards = bp.section("characters")
    names = [next((c.get("name") or cid for c in cards if c.get("id") == cid), cid)
             for cid in gist.get("characters") or []]
    md = render_gist_md(gist, vol, ch, names)
    p = ctx.ws.outline_chapter_path(ctx.project_id, vol, ch)
    ctx.ws.write_text(p, md)
    return []


# ---- 节点执行 ----
_PROMPTS: dict[str, Callable[[NodeContext], tuple[str, str]]] = {
    "book": _book_prompt,
    "volume": _volume_prompt,
    "chapter": _chapter_prompt,
    "worldview": _worldview_prompt,
    "system": _system_prompt,
    "setting_entry": _setting_entry_prompt,
    "character_group": _character_group_prompt,
    "character": _character_prompt,
    "style": _style_prompt,
    "thread_set": _thread_set_prompt,
    "arc": _arc_prompt,
    "beat": _beat_prompt,
}
_APPLY: dict[str, Callable[[NodeContext, dict], list[str]]] = {
    "book": _apply_book,
    "volume": _apply_volume,
    "chapter": _apply_chapter,
    "worldview": _apply_worldview,
    "system": _apply_system,
    "setting_entry": _apply_setting_entry,
    "character_group": _apply_character_group,
    "character": _apply_character,
    "style": _apply_style,
    "thread_set": _apply_thread_set,
    "arc": _apply_arc,
    "beat": _apply_beat,
}


def _node_id_of(kind: str, ctx: NodeContext) -> str:
    if kind == "book":
        return "book"
    if kind in ("worldview", "character_group", "style", "thread_set"):
        return kind
    if kind in ("chapter", "beat"):
        return f"{kind}:{ctx.vol}:{ctx.ch}"
    if kind == "arc":
        return f"arc:{ctx.vol}:{str((ctx.child or {}).get('id') or '')}"
    if kind in ("system", "setting_entry", "character"):
        return f"{kind}:{str((ctx.child or {}).get('id') or '')}"
    return f"{kind}:{ctx.vol}"


def _ensure_json_hint(system: str, user: str) -> tuple[str, str]:
    """DeepSeek 兼容：response_format=json_object 要求 prompt 含 "json" 字样（OpenAI 无此约束）。

    run_node 统一对全部节点走 json_object；个别节点 prompt（如 worldview/character_group）
    未在模板里写 "json" → DeepSeek 400 回退。此处按需给 user prompt 追加 JSON 引导，
    只影响不含 "json" 字样的调用，既有成功路径行为不变（D1，2026-09-03 新书实测）。
    """
    if "json" in (system + user).lower():
        return system, user
    hint = ("\n\n请以 JSON 对象返回结果（response_format=json_object）："
            "整体是一个以 { 开头、} 结尾的 JSON 对象，键与字符串值均用双引号，"
            "不要输出任何解释、代码块标记或 JSON 之外的文字。")
    return system, user + hint


def run_node(ctx: NodeContext, kind: str) -> NodeResult:
    """执行一个节点：prompt → LLM → 解析（协议）→ apply。抛 ValueError = 解析失败（引擎重试）。"""
    if kind not in _PROMPTS:
        raise ValueError(f"unknown node kind: {kind}")
    node_id = _node_id_of(kind, ctx)
    system, user = _ensure_json_hint(*_PROMPTS[kind](ctx))
    res = ctx.provider.complete(LLMRequest(
        messages=[LLMMessage(role="system", content=system),
                  LLMMessage(role="user", content=user)],
        temperature=0.5, max_tokens_out=2600, response_format="json_object"))
    if res.blocked:
        from ..core.llm import ModerationBlockedError

        raise ModerationBlockedError(res.block_reason, res.provider_note)
    if not (res.content or "").strip():
        raise ValueError("empty node reply")
    node = _parse_node_reply(res.content)
    warns = ctx.extra_warnings + _APPLY[kind](ctx, node)
    if node["decide"] == "expand" and not node["children"]:
        warns.append(f"{node_id}: decide=expand 但未给 children，按 done 处理")
    if node["has_shell"] and not node["reason"]:
        warns.append(f"{node_id}: 模型未给 reason（协议要求必填）")
    usage = getattr(res, "usage", None)
    return NodeResult(kind=kind, node_id=node_id, ok=True, artifact=node["artifact"],
                      decide=node["decide"], reason=node["reason"],
                      children=node["children"], warnings=warns,
                      tokens_in=int(getattr(usage, "tokens_in", 0) or 0),
                      tokens_out=int(getattr(usage, "tokens_out", 0) or 0))


# ---- 确定性落盘（零 LLM）----
def sync_bible(ws: Workspace, project_id: str, bp: Blueprint) -> list[str]:
    """蓝图 → bible 文件全量重写（剥离 role、补默认字段、主角引用）。返回写入的相对路径。"""
    written: list[str] = []

    def write(rel: str, data: Any) -> None:
        ws.write_json(ws._abs(f"{project_id}/{rel}"), data)  # noqa: SLF001
        written.append(rel)

    # worldview
    wv = dict(bp.get("worldview") or {})
    wv.setdefault("id", "world:main")
    wv.setdefault("name", (bp.get("meta") or {}).get("title") or "未命名世界")
    # factions 归一化：蓝图是字符串数组（中间态），bible schema 要求对象数组（知识单元）
    if wv.get("factions"):
        norm = []
        for f in wv["factions"]:
            if isinstance(f, str):
                norm.append({"faction": f})
            elif isinstance(f, dict) and f.get("faction"):
                norm.append(f)
        wv["factions"] = norm
    write("bible/worldview.json", wv)
    # characters（剥离 role；protagonist → is_protagonist）
    chars = []
    for c in bp.section("characters"):
        card = {k: v for k, v in c.items() if k != "role"}
        card.setdefault("status", "active")
        if c.get("role") == "protagonist":
            card["is_protagonist"] = True
        chars.append(card)
    write("bible/characters.json", chars)
    # style（补 protagonist 引用）
    st = dict(bp.get("style") or {})
    proto = next((c for c in bp.section("characters") if c.get("role") == "protagonist"), None)
    if proto:
        st["protagonist"] = {"id": proto["id"], "name": proto.get("name"), "gender": proto.get("gender")}
    write("bible/style.json", st)
    # threads（补默认 status/scope；白名单过滤——bible schema items additionalProperties=false，
    # plant_desc/payoff_desc 已收入 schema（F5 修订：原过滤丢弃导致伏笔语义丢失，F4 测试证伪））
    threads = []
    for t in bp.section("threads"):
        row = dict(t)
        row.setdefault("status", "unplanned")
        row.setdefault("scope", "book")
        threads.append({k: v for k, v in row.items()
                        if k in ("id", "desc", "scope", "target_vol", "planted",
                                 "status", "report_deadline", "returned", "revision",
                                 "plant_desc", "payoff_desc")})
    write("bible/plot_threads.json", threads)
    # 有内容才写其余段（同样白名单过滤）
    for section, rel, keep in (("locations", "bible/locations.json",
                                {"id", "name", "parent", "desc", "status", "revision", "aliases"}),
                               ("items", "bible/items.json",
                                {"id", "name", "type", "aliases", "state", "desc", "note"}),
                               ("skills", "bible/skills.json",
                                {"id", "name", "type", "aliases", "state", "note"}),
                               ("settings", "bible/settings.json",
                                {"id", "keywords", "text", "revealed", "first_ch"})):
        data = bp.section(section)
        if data:
            rows = [{k: v for k, v in x.items() if k in keep}
                    for x in data if isinstance(x, dict)]
            if section == "items":  # D3：category/kind 白名单外会被剥离，type 在此归一化补齐
                for src, row in zip((x for x in data if isinstance(x, dict)), rows):
                    row["type"] = _normalize_item_type(src)
            write(rel, rows)
    return written


def synthesize_worldstate(bp: Blueprint) -> dict:
    """worldstate 确定性合成（docs/10 §7.5 末行，零 LLM、不进构建树）。

    time={now:0, origin_text}；characters 初始状态由蓝图 power/arc/faction 合成；
    pending：细纲章 gist 可选 `after_days`（ADR-019，docs/10 §4.2）→ 定时事件登记
    （M3m T4）：due = 此前各章 after_days 累计（相对天数轴，now 从 0 起），
    id 用 `pd:ke-<vol>-<ch>` 与生成期数字 id（pd:N）不冲突。字段与
    timeline.add_pending 对齐，渐进提醒分档开箱可用。
    """
    meta = bp.get("meta") or {}
    chars: dict[str, dict] = {}
    for c in bp.section("characters"):
        cid, name = c.get("id"), c.get("name")
        if not cid or not name:
            continue
        pw = c.get("power") or {}
        entry: dict[str, Any] = {
            "name": name,
            "realm": pw.get("level") or "",
            "location": "",
            "items": [],
            "injuries": [],
            "dead": False,
            "history": [],
        }
        chars[cid] = entry
    pending: list[dict] = []
    t = 0
    chapters = sorted(bp.section("chapters"),
                      key=lambda x: (int(x.get("vol") or 1), int(x.get("ch") or 0)))
    for g in chapters:
        n = int(g.get("after_days") or 0)
        t += n
        if n <= 0:
            continue
        vol, ch = int(g.get("vol") or 1), int(g.get("ch") or 0)
        events = [str(e) for e in (g.get("key_events") or []) if str(e).strip()]
        pending.append({
            "id": f"pd:ke-{vol}-{ch}",
            "who": "",                                  # 世界级日程（schema 允许空串）
            "what": events[0] if events else str(g.get("title") or f"第 {ch} 章"),
            "due": t,
            "span": max(t, 1),                          # 登记时 now=0 → 跨度=due
            "created_t": 0,
            "status": "scheduled",
            "created_at": {"vol": vol, "ch": ch},
            "overdue": 0,
            "block_count": 0,
        })
    return {
        "time": {"now": 0, "origin_text": meta.get("time_origin") or "开书之日"},
        "pending": pending,
        "characters": chars,
    }
