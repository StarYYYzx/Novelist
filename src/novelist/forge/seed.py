"""模式一 `seed`（docs/10 §5）：一句话 → 蓝图 → 构建（M3l F1/F2）。

流程（docs/08 F1/F2 表格）：
1. **种子提炼**（1 次 LLM）：brief + 可用 Genre Pack 清单 → `SeedSpec`。
   解析失败降级为确定性兜底（genre/scale 用 CLI 参数与包默认），记 warn、写 transcript。
2. **蓝图初始化**：SeedSpec → blueprint meta / 主角骨架 / 包默认值（src=template）。
   CLI 显式参数（--volumes 等）视为用户输入，provenance=user——受保护，永不被模型覆盖。
3. **授权询问**（interactive 且 TTY）：展示提炼结果，问 [1] 全权 / [2] 商讨。
   非 TTY 自动降级 auto + 告警写 transcript（docs/10 §5.3）。选 [2] 进 `run_consult`
   （F2：分轮问答，q 提前结束则留缺口等 resume/build）。
4. **构建**：调 `engine.build`（最小树 book→volume→chapter，卷闸门 vol=1）。
   构建后蓝图可手工编辑，`forge build` 重跑（provenance 保护 src=user）。
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..core.llm import LLMMessage, LLMRequest
from ..core.output import emit
from ..storage.workspace import Workspace
from . import genres as _genres
from .state import Blueprint, ForgeState, append_transcript

DEFAULT_VOLUMES = 3
DEFAULT_CHAPTERS = 20
DEFAULT_WORDS = 2400
# FIX-1 scale sanity：LLM 推测规模（卷×章）超此值即钳回默认并降置信（用户显式给的不受限）
SCALE_SANITY_LIMIT = 100


@dataclass
class SeedSpec:
    """种子提炼协议（docs/10 §5.1）。"""

    genre: str = ""
    template_suggestion: str = ""
    logline: str = ""
    protagonist_hint: dict = field(default_factory=dict)
    conflict: str = ""
    tone_hint: str = ""
    scale_hint: dict = field(default_factory=dict)
    time_origin: str = ""
    anchors: list[str] = field(default_factory=list)  # D13：brief 硬约束锚点（原话摘抄）
    unknowns: list[str] = field(default_factory=list)


@dataclass
class SeedResult:
    """seed 结果（构建统计 + 提示）。"""

    ok: bool
    project_id: str
    spec: SeedSpec
    mode_used: str  # auto | interactive
    warnings: list[str] = field(default_factory=list)
    build: dict = field(default_factory=dict)  # engine.BuildResult.to_dict()
    blueprint_path: str = ""
    quit_early: bool = False  # 商讨 q 退出（已答落蓝图，未答留缺口）


# ---- 种子提炼 ----
SEED_SYSTEM = """你是网文立项编辑。根据用户的一句话创意，提炼立项要素，供构建引擎使用。
只输出 JSON，不要任何解释或前后缀。"""


def _seed_user_prompt(brief: str, packs: list[str], genre_pack: str | None) -> str:
    pack_lines = "\n".join(f"  - {p}" for p in packs) or "  - （无）"
    hint = f"（用户指定模板：{genre_pack}，优先采用）" if genre_pack else ""
    return f"""可用类型模板：\n{pack_lines}\n{hint}
用户创意一句话：{brief}

按以下 JSON 输出（键名严格一致，缺失的键填空串/空数组）：
{{
  "genre": "类型名（优先匹配上方模板清单中的 id 或别名；无则给最贴近的自拟名，如「东方蒸汽朋克」）",
  "template_suggestion": "匹配到的模板 id（无则空串）",
  "logline": "一句话卖点（核心冲突 + 最大看点）",
  "protagonist_hint": {{"name": "主角名（原话没给则空串）", "gender": "male|female|unknown", "cheat": "金手指/核心机制一句话"}},
  "conflict": "核心冲突一句话",
  "tone_hint": "文风基调提示（如：热血激昂/严谨冷肃/诙谐幽默）",
  "scale_hint": {{"volumes": 卷数, "chapters_per_volume": 每卷章数}},
  "time_origin": "故事时间原点（t=0 的语义锚点，如「主角穿越之日」；没有则空串）",
  "anchors": ["从创意原句中原样摘抄的硬性设定锚点（数字/时间跨度/年龄/事实因果），每条一字不改，如「灵气复苏仅过去了一年」「大学生，23 岁」；原句没有则空数组"],
  "unknowns": ["这句话里你无法确定、需要后续澄清的要素，每条一句"]
}}"""


def _parse_seed_spec(raw: str) -> SeedSpec:
    """宽松解析提炼输出；结构不对时抛 ValueError（调用方降级）。"""
    text = raw.strip()
    # 剥掉可能的 ```json 围栏
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no json object in seed reply")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("seed reply not an object")
    ph = data.get("protagonist_hint") or {}
    if not isinstance(ph, dict):
        ph = {}
    sh = data.get("scale_hint") or {}
    if not isinstance(sh, dict):
        sh = {}
    return SeedSpec(
        genre=str(data.get("genre") or "").strip(),
        template_suggestion=str(data.get("template_suggestion") or "").strip(),
        logline=str(data.get("logline") or "").strip(),
        protagonist_hint={
            "name": str(ph.get("name") or "").strip(),
            "gender": str(ph.get("gender") or "unknown").strip(),
            "cheat": str(ph.get("cheat") or "").strip(),
        },
        conflict=str(data.get("conflict") or "").strip(),
        tone_hint=str(data.get("tone_hint") or "").strip(),
        scale_hint={
            "volumes": int(sh["volumes"]) if sh.get("volumes") else 0,
            "chapters_per_volume": int(sh["chapters_per_volume"]) if sh.get("chapters_per_volume") else 0,
        },
        time_origin=str(data.get("time_origin") or "").strip(),
        anchors=[str(x).strip() for x in (data.get("anchors") or []) if str(x).strip()],
        unknowns=[str(x).strip() for x in (data.get("unknowns") or []) if str(x).strip()],
    )


def _fallback_spec(brief: str, genre_pack: str | None, volumes: int | None,
                   chapters_per_volume: int | None) -> SeedSpec:
    """提炼失败/被拦截时的确定性兜底（docs/10 §12：降级不静默）。"""
    return SeedSpec(
        genre=genre_pack or "修仙",
        template_suggestion=genre_pack or "",
        logline=brief.strip()[:80],
        conflict=brief.strip()[:80],
        scale_hint={
            "volumes": volumes or DEFAULT_VOLUMES,
            "chapters_per_volume": chapters_per_volume or DEFAULT_CHAPTERS,
        },
    )


def _slug(name: str, fallback: str) -> str:
    """名字 → id 片段（ASCII 小写；中文字符丢弃）。"""
    s = re.sub(r"[^A-Za-z0-9_]", "", name.lower())
    return s or fallback


def _scale_of(spec: SeedSpec, volumes: int | None, chapters_per_volume: int | None,
              target_words: int | None, pack: dict) -> dict:
    v = volumes or (spec.scale_hint.get("volumes") or DEFAULT_VOLUMES)
    c = chapters_per_volume or (spec.scale_hint.get("chapters_per_volume") or DEFAULT_CHAPTERS)
    w = target_words or (pack.get("default_style") or {}).get("target_words_per_chapter") or DEFAULT_WORDS
    return {"volumes": int(v), "chapters_per_volume": int(c), "target_words_per_chapter": int(w)}


def _init_blueprint(ws: Workspace, project_id: str, brief: str, spec: SeedSpec,
                    pack: dict, pack_id: str,
                    volumes: int | None, chapters_per_volume: int | None,
                    target_words: int | None,
                    craft: list[str] | None = None,
                    warnings: list[str] | None = None) -> Blueprint:
    """SeedSpec + 包默认 → 蓝图（provenance 分层：user > llm > template）。

    ``warnings`` 由调用方（``run_seed``）传入用于收集告警；未传时本次告警丢弃而不报错
    （修复：此前 245 行直接引用 ``warnings`` 自由变量，而本函数内并无该名字——
    定义在另一函数 ``run_seed`` 中，传入未知工艺卡 id 时会抛 NameError）。
    """
    _warnings = warnings if warnings is not None else []
    bp = Blueprint.blank()
    meta = bp.data["meta"]

    # 用户显式参数 → user（受保护）；其余按 llm/template 分层
    def set_meta(key: str, value: Any, src: str, conf: float = 1.0) -> None:
        meta[key] = value
        bp.set_provenance(f"meta.{key}", src, conf)

    set_meta("title", brief.strip()[:24] or f"《{project_id}》", "user", 1.0)
    set_meta("brief", brief, "user", 1.0)
    set_meta("genre", spec.genre or pack_id, "llm", 0.8)
    set_meta("template", pack_id, "template", 1.0)
    set_meta("logline", spec.logline or brief.strip()[:80], "llm", 0.8)
    scale = _scale_of(spec, volumes, chapters_per_volume, target_words, pack)
    src = "user" if (volumes or chapters_per_volume or target_words) else "llm"
    # FIX-1（scale sanity，L6 实证：一句话被 LLM 自由发挥成 6×100=600 章）：
    # 推测规模超阈值 → 钳回默认并**降置信**（0.4 < meta.scale 槽位阈值 0.5，
    # detect_gaps 会列为 low_confidence 缺口——商讨轮/对话 agent 都会来确认）；
    # 用户显式给规模（src=user）永不钳制。
    if (src == "llm"
            and scale["volumes"] * scale["chapters_per_volume"] > SCALE_SANITY_LIMIT):
        _warnings.append(
            f"规模 {scale['volumes']}×{scale['chapters_per_volume']} 超出常理"
            f"（一句话创意不宜直接规划超长篇），已钳制为默认 "
            f"{DEFAULT_VOLUMES}×{DEFAULT_CHAPTERS}；确需更大规模请显式指定")
        scale = {"volumes": DEFAULT_VOLUMES, "chapters_per_volume": DEFAULT_CHAPTERS,
                 "target_words_per_chapter": scale["target_words_per_chapter"]}
        conf = 0.4
    else:
        conf = 1.0 if src == "user" else 0.8
    meta["scale"] = scale
    bp.set_provenance("meta.scale", src, conf)
    if spec.time_origin:
        set_meta("time_origin", spec.time_origin, "llm", 0.8)

    # D13：硬约束锚点——LLM 原话摘抄为候选，正则兜底补漏（数字/中文数字+单位，
    # 取所在短句原话），并查重。下游所有 forge prompt 以「不得改动」注入，
    # 防"一年"被改写为"第三年"。
    anchors = list(dict.fromkeys(spec.anchors))
    import re as _re

    _num = r"(?:\d+|[一二两三四五六七八九十百千零]+)\s*(?:个\s*)?(?:年|月|天|日|岁|层|阶|倍|米|公里|里|名|位|座|枚|颗)"
    for m in _re.finditer(_num, brief):
        # 锚点=含该数量词的完整短句（按标点切），一字不改
        seg = _re.split(r"[，。！？；;、]", brief)
        hit = next((s.strip() for s in seg if m.group(0) in s), None)
        if hit and hit not in anchors:
            anchors.append(hit)
    if anchors:
        set_meta("anchors", anchors[:12], "user", 0.9)

    # worldview：只装包默认（template）。FIX-1（2026-09-19 方案 / 2026-10-24 落地）：
    # 金手指（系统/异能）是**主角的所有物**，不再写进 worldview.power_system.mechanic——
    # 此前 cheat 的唯一载体成了世界观，book 节点把它 elaboration 成多条世界铁律，
    # 正文全篇零"系统"字样（proj-20260919091817 实证）。cheat 改落主角卡与
    # meta.protagonist_hint（见下）。
    wv = bp.data["worldview"]
    wv["power_system"] = {
        "mechanic": (pack.get("power_system") or {}).get("mechanic", ""),
        "levels": list((pack.get("power_system") or {}).get("levels") or []),
    }
    if wv["power_system"]["mechanic"]:
        bp.set_provenance("worldview.power_system.mechanic", "template", 1.0)
    if wv["power_system"]["levels"]:
        bp.set_provenance("worldview.power_system.levels", "template", 1.0)

    # style：包默认（template）+ 提炼基调（llm 覆盖 template）
    st = bp.data["style"]
    ds = pack.get("default_style") or {}
    tone = []
    if spec.tone_hint:
        tone = [spec.tone_hint]
        bp.set_provenance("style.tone", "llm", 0.8)
    elif ds.get("tone"):
        tone = list(ds["tone"])
        bp.set_provenance("style.tone", "template", 1.0)
    st["tone"] = tone
    st["pov"] = ds.get("pov", "第三人称限知（主角视角）")
    st["target_words_per_chapter"] = scale["target_words_per_chapter"]
    st["forbidden_words"] = list(pack.get("default_banned") or [])
    bp.set_provenance("style.pov", "template", 1.0)
    bp.set_provenance("style.target_words_per_chapter", "template", 1.0)
    bp.set_provenance("style.forbidden_words", "template", 1.0)

    # 题材工艺卡（craft）：设定阶段勾选的硬规范 id 列表。
    # 动机见 src/novelist/craft/__init__.py——风格解释权不能全交模型。
    st.setdefault("craft_cards", [])
    if craft:
        from ..craft.loader import valid_ids

        known = set(valid_ids())
        picked, unknown = [], []
        for cid in craft:
            s = str(cid).strip()
            if not s:
                continue
            (picked if s in known else unknown).append(s)
        if unknown:
            _warnings.append(
                f"未知工艺卡 id：{'、'.join(unknown)}（已忽略；可用：{'、'.join(sorted(known))}）")
        if picked:
            st["craft_cards"] = picked
            bp.set_provenance("style.craft_cards", "user", 1.0)
    else:
        # DL-015（2026-10-24）：类型包默认卡——未显式勾选时自动带上（防新用户漏勾，
        # 如系统流忘勾系统卡），provenance=template（商讨/对话里可增删覆盖）
        for cid in (pack.get("default_cards") or []):
            s = str(cid).strip()
            if s and s not in st["craft_cards"]:
                st["craft_cards"].append(s)
    # DL-015⑦ 工艺卡重定位：卡（通用知识）+ 本书拍板（meta.romance 等）→
    # **确定性实例化**为 style.craft_rules（本书规则）——生成期注入与编译期
    # 判定的都是这份实例化产物；卡的 INJECT 段降级为模板。详见 craft/instantiate.py。
    from ..craft.instantiate import instantiate_craft_rules

    crules = instantiate_craft_rules(st, meta)
    if crules:
        st["craft_rules"] = crules
        bp.set_provenance("style.craft_rules", "user" if craft else "template", 1.0)

    # 主角线索：FIX-1——hint **持久化进蓝图 meta**（此前提炼完即丢，下游只能从
    # 世界观反推主角机制）；cheat 同步写进主角骨架卡（人物卡 schema 已加 cheat 字段）。
    ph = spec.protagonist_hint or {}
    name = str(ph.get("name") or "").strip()
    cheat = str(ph.get("cheat") or "").strip()
    gender = ph.get("gender") if ph.get("gender") in ("male", "female") else "unknown"
    meta["protagonist_hint"] = {"name": name, "gender": gender, "cheat": cheat}
    bp.set_provenance("meta.protagonist_hint", "llm", 0.8)
    # 主角骨架（提炼给名才建档；book 节点会补全/兜底；不预置泛型 char:protagonist 卡）
    if name:
        cid = f"char:{_slug(name, 'protagonist')}"
        card = {
            "id": cid,
            "name": name,
            "gender": gender,
            "role": "protagonist",
            "core_traits": [],
            "status": "active",
        }
        if cheat:
            card["cheat"] = cheat
            bp.set_provenance(f"characters[{cid}].cheat", "llm", 0.8)
        bp.upsert("characters", card)
        bp.set_provenance(f"characters[{cid}].name", "llm", 0.8)
        bp.set_provenance(f"characters[{cid}].gender", "llm", 0.8)
        bp.set_provenance(f"characters[{cid}].role", "template", 1.0)
    return bp


# ---- 授权询问（docs/10 §5.2，仅一次）----
def _authorize_ask(bp: Blueprint, spec: SeedSpec, brief: str) -> str:
    """交互询问 [1] 全权 / [2] 商讨；返回 'auto' | 'consult'（F2）。"""
    meta = bp.get("meta") or {}
    scale = meta.get("scale") or {}
    ph = spec.protagonist_hint or {}
    lines = [
        f"根据「{brief}」我理解为：",
        f"  流派：{meta.get('genre', '—')}（模板：{meta.get('template', '—')}）",
        f"  卖点：{meta.get('logline', '—')}",
        f"  主角：{ph.get('name') or '（原话未给，稍后补全）'}，"
        f"{'男' if ph.get('gender') == 'male' else '女' if ph.get('gender') == 'female' else '未知'}，"
        f"金手指：{ph.get('cheat') or '—'}",
        f"  规模：{scale.get('volumes', '?')} 卷 × {scale.get('chapters_per_volume', '?')} 章 × "
        f"{scale.get('target_words_per_chapter', '?')} 字",
    ]
    if spec.unknowns:
        lines.append(f"  不确定的：{'、'.join(spec.unknowns[:4])}"
                     + ("…" if len(spec.unknowns) > 4 else ""))
    lines += [
        "",
        "接下来怎么构建？",
        "  [1] 全权构建 —— 我按流派模板补全全部设定，最后给你一份清单过目",
        "  [2] 商讨构建 —— 分 3–4 轮问你关键设定，每问都给候选值",
        "选择 [1/2]（回车默认 [1]）：",
    ]
    emit("\n".join(lines))
    try:
        ans = input().strip()
    except EOFError:
        return "auto"
    return "consult" if ans == "2" else "auto"


# ---- 主入口 ----
def run_seed(ws: Workspace, project_id: str, brief: str, **kw: Any) -> "SeedResult":
    """一句话 → 蓝图 → 构建（薄包装）：给原始调用日志挂 forge:seed 上下文（AskUser 时叠加）。"""
    from ..core.calllog import call_context

    with call_context("forge:seed"):
        return _run_seed_impl(ws, project_id, brief, **kw)


def _run_seed_impl(
    ws: Workspace, project_id: str, brief: str, *,
             provider, mode: str = "auto", genre_pack: str | None = None,
             volumes: int | None = None, chapters_per_volume: int | None = None,
             target_words: int | None = None,
             max_calls: int | None = None, max_depth: int = 4, max_width: int = 4,
             max_width_list: int = 12,  # ADR-033 A
             craft: list[str] | None = None,
             smoke: bool = False, deepen: bool = True, gate: bool = True,
             doctor: bool = True,
             ask_fn: Callable[[Blueprint, SeedSpec, str], str] | None = None) -> SeedResult:
    """一句话 → 蓝图 → 构建。mode=interactive 且 TTY 时先授权询问。

    `ask_fn` 返回 'auto'（全权）或 'consult'（商讨）。`smoke=True`：只提炼 + 建蓝图，
    不跑构建（CLI --smoke，docs/10 §11）。
    `craft`：题材工艺卡 id 列表（src/novelist/craft/cards/*.md），写入
    style.craft_cards（provenance=user），正文生成时注入其规范段。
    """
    warnings: list[str] = []
    state = ForgeState.load(ws, project_id)
    state.mode = "seed"
    state.interaction = mode
    state.stage = "seed"
    state.started_at = state.started_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state.save(ws, project_id)
    append_transcript(ws, project_id, "seed.start", brief=brief[:200], mode=mode, genre_pack=genre_pack)

    # 1) 种子提炼
    spec: SeedSpec | None = None
    _SEED_RETRIES = 2   # 思考模式抖动（根因同 broadcast B1）：flash 偶发 thinking 吃满 → content 空，重试收敛
    try:
        pack_list = _genres.list_packs()
        last_err = "seed 提炼返回空内容"
        from ..core.output import emit, heartbeat  # UX-2：提炼期可见性

        emit("[seed] 提炼中（一句话 → 立项要素）…")
        for _ in range(_SEED_RETRIES + 1):
            try:
                with heartbeat("seed 提炼"):
                    res = provider.complete(LLMRequest(
                          messages=[LLMMessage(role="system", content=SEED_SYSTEM),
                                    LLMMessage(role="user", content=_seed_user_prompt(brief, pack_list, genre_pack))],
                          temperature=0.4, max_tokens_out=4000, response_format="json_object",
                          thinking=True))  # 判断类：种子结构化归纳，开思考
                if res.blocked:
                    raise RuntimeError(f"审核拦截: {res.block_reason or 'unknown'}")
                if not (res.content or "").strip():
                    raise RuntimeError("seed 提炼返回空内容")
                spec = _parse_seed_spec(res.content)
                break
            except Exception as e:  # noqa: BLE001 - 单次尝试失败则重试，重试尽交给外层兜底
                last_err = str(e)
        if spec is None:
            raise RuntimeError(last_err)
    except Exception as e:  # noqa: BLE001 - 提炼失败降级兜底（docs/10 §12）
        warnings.append(f"seed 提炼失败，使用确定性兜底: {e}")
        spec = _fallback_spec(brief, genre_pack, volumes, chapters_per_volume)
        append_transcript(ws, project_id, "seed.fallback", error=str(e)[:200])

    # 2) 蓝图初始化
    pack_id = genre_pack or spec.template_suggestion
    try:
        pack = _genres.load_pack_for(pack_id) if pack_id else _genres.load_pack(_genres.GENERIC_ID)
        pack_id = pack.get("id") or pack_id  # 回填实际装载的包 id（含别名/兜底）
    except KeyError:
        pack = _genres.load_pack(_genres.GENERIC_ID)
        pack_id = _genres.GENERIC_ID
        warnings.append(f"模板 {genre_pack or spec.template_suggestion!r} 不存在，装载通用包")
    bp = _init_blueprint(ws, project_id, brief, spec, pack, pack_id,
                         volumes, chapters_per_volume, target_words, craft,
                         warnings=warnings)
    bp.save(ws, project_id)
    append_transcript(ws, project_id, "seed.blueprint", rev=bp.data["rev"],
                      genre=pack_id, scale=bp.get("meta.scale"))

    # 3) 授权询问（interactive 且 TTY；否则降级 auto）——[2] 商讨进入 run_consult（F2）
    mode_used = mode
    consult = None
    if mode == "interactive":
        tty = sys.stdin.isatty() and sys.stdout.isatty()
        if tty:
            ask = ask_fn or _authorize_ask
            choice = ask(bp, spec, brief)
            mode_used = "interactive"
            if choice == "consult":
                from .ask import run_consult
                from .io_console import ConsoleIO
                from .slots import slots_for_genre

                state.touch_stage(ws, project_id, "consulting")
                consult = run_consult(ws, project_id, bp, provider=provider, io=ConsoleIO(),
                                      slots=slots_for_genre(pack))
                warnings += consult.warnings
                if not consult.quit_early:
                    state.touch_stage(ws, project_id, "seeded")
        else:
            warnings.append("非交互环境：interactive 降级为 auto（全部取推荐值）")
            append_transcript(ws, project_id, "seed.downgrade", reason="no-tty")
            mode_used = "auto"
    state.interaction = mode_used
    state.save(ws, project_id)

    # 商讨中途 q 退出：已答落蓝图 + transcript，未答留缺口——不构建，等 resume/build
    if consult is not None and consult.quit_early:
        state.stage = "consulting"
        state.save(ws, project_id)
        warnings.append(
            f"商讨提前结束（已答 {consult.answered} 项 / 自由答案 {consult.free_answers} 项，"
            f"完成 {consult.rounds_done} 轮）。"
            "用 `forge resume` 继续商讨，或 `forge build` 直接构建。"
        )
        return SeedResult(
            ok=True, project_id=project_id, spec=spec, mode_used=mode_used,
            warnings=warnings, build={"calls_used": 0, "ok": False},
            blueprint_path=str(ws._abs(f"{project_id}/workspace/forge/blueprint.json")),  # noqa: SLF001
            quit_early=True,
        )

    # 4) 全权构建（最小树）；smoke 模式只提炼 + 建蓝图
    if smoke:
        state.stage = "seed"
        state.save(ws, project_id)
        return SeedResult(
            ok=True, project_id=project_id, spec=spec, mode_used=mode_used,
            warnings=warnings, build={"calls_used": 0, "ok": True},
            blueprint_path=str(ws._abs(f"{project_id}/workspace/forge/blueprint.json")),  # noqa: SLF001
        )
    from .engine import build  # 延迟导入，避免 seed↔engine 顶层循环

    build_res = build(ws, project_id, provider=provider,
                      max_calls=max_calls, max_depth=max_depth, max_width=max_width,
                      max_width_list=max_width_list,
                      deepen=deepen, gate=gate, doctor=doctor)
    if not build_res.ok and not warnings and not build_res.warnings:
        warnings.append("构建未完成，见 build.warnings")

    return SeedResult(
        ok=build_res.ok,
        project_id=project_id,
        spec=spec,
        mode_used=mode_used,
        warnings=warnings + build_res.warnings,
        build=build_res.to_dict(),
        blueprint_path=str(ws._abs(f"{project_id}/workspace/forge/blueprint.json")),  # noqa: SLF001
    )
