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

KINDS = ("book", "volume", "chapter")


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
    extra_warnings: list[str] = field(default_factory=list)


@dataclass
class NodeResult:
    kind: str
    node_id: str
    ok: bool
    artifact: dict | None = None
    decide: str = "done"
    reason: str = ""
    warnings: list[str] = field(default_factory=list)


def _parse_node_reply(raw: str) -> dict:
    """宽松解析节点协议 JSON；结构不对抛 ValueError（引擎重试/回退）。"""
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
    art = data.get("artifact")
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
  "items": [{"id": "item:xxx", "name": "物品名", "category": "类别", "desc": "描述"}],
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
    cards = bp.section("characters")
    char_block = json.dumps([
        {k: c.get(k) for k in ("id", "name", "role", "gender", "core_traits", "power") if c.get(k)}
        for c in cards
    ], ensure_ascii=False, indent=2) or "（无角色卡——book 节点应先产出）"
    rhythm = (ctx.pack or {}).get("chapter_rhythm") or ["opening-hook", "setup", "conflict", "climax", "cliffhanger"]
    user = f"""你是细纲师。写第 {vol} 卷第 {ch} 章的章节细纲（全书 {scale.get('chapters_per_volume', '?')} 章/卷）。

【本卷主线】{vol_block}

【前一章（因果连续，必须衔接）】
{prev_block}

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


# ---- apply（落盘 + 蓝图回写，provenance 保护）----
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

    # worldview：合并（保护 user 字段；power_system 二级路径逐键保护）
    wv = dict(bp.get("worldview") or {})
    for k in ("name", "power_system", "rules", "factions", "civilizations"):
        v = _dict_of(art.get("worldview")).get(k)
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

    # style（保护 user 字段；glossary 按 term 去重合并）
    st = dict(bp.get("style") or {})
    style_art = _dict_of(art.get("style"))
    for k in ("pov", "tense", "narration", "tone"):
        v = style_art.get(k)
        if v in (None, ""):
            continue
        if isinstance(v, list) and not v:
            continue
        if bp.is_protected(f"style.{k}"):
            continue
        st[k] = v
        bp.set_provenance(f"style.{k}", "llm", 0.8)
    gloss = style_art.get("glossary")
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
    }
    parts = [
        "---",
        json.dumps(fm, ensure_ascii=False),
        "---",
        "",
        f"# 第 {ch} 章 {fm['title']}",
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


# ---- 节点执行 ----
_PROMPTS: dict[str, Callable[[NodeContext], tuple[str, str]]] = {
    "book": _book_prompt,
    "volume": _volume_prompt,
    "chapter": _chapter_prompt,
}
_APPLY: dict[str, Callable[[NodeContext, dict], list[str]]] = {
    "book": _apply_book,
    "volume": _apply_volume,
    "chapter": _apply_chapter,
}


def run_node(ctx: NodeContext, kind: str) -> NodeResult:
    """执行一个节点：prompt → LLM → 解析（协议）→ apply。抛 ValueError = 解析失败（引擎重试）。"""
    if kind not in _PROMPTS:
        raise ValueError(f"unknown node kind: {kind}")
    node_id = f"{kind}:{ctx.vol}:{ctx.ch}" if kind == "chapter" else (f"{kind}:{ctx.vol}" if kind == "volume" else "book")
    system, user = _PROMPTS[kind](ctx)
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
    if node["decide"] == "expand" and node["children"]:
        # F1：树形由引擎确定性展开；模型 expand 的 children（arc 层）F4 提供，仅留痕
        warns.append(f"{node_id}: 模型请求细化（decide=expand, {len(node['children'])} children）"
                     "——arc/beat 层 F4 提供，本版按当前层级继续")
    if not node["reason"]:
        warns.append(f"{node_id}: 模型未给 reason（协议要求必填）")
    return NodeResult(kind=kind, node_id=node_id, ok=True, artifact=node["artifact"],
                      decide=node["decide"], reason=node["reason"], warnings=warns)


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
    # threads（补默认 status/scope）
    threads = []
    for t in bp.section("threads"):
        row = dict(t)
        row.setdefault("status", "unplanned")
        row.setdefault("scope", "book")
        threads.append(row)
    write("bible/plot_threads.json", threads)
    # 有内容才写其余段
    for section, rel in (("locations", "bible/locations.json"), ("items", "bible/items.json"),
                         ("skills", "bible/skills.json"), ("settings", "bible/settings.json")):
        data = bp.section(section)
        if data:
            write(rel, data)
    return written


def synthesize_worldstate(bp: Blueprint) -> dict:
    """worldstate 确定性合成（docs/10 §7.5 末行，零 LLM、不进构建树）。

    time={now:0, origin_text}；characters 初始状态由蓝图 power/arc/faction 合成；
    pending/unavailable 为空（构建期无定时事件登记）。
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
    return {
        "time": {"now": 0, "origin_text": meta.get("time_origin") or "开书之日"},
        "pending": [],
        "characters": chars,
    }
