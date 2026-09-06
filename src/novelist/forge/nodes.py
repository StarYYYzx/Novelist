"""构建引擎节点层（docs/10 §7.2 / §7.5 / §4.2 B3）。

F1 最小树三个节点：
- **book**：设定骨架（worldview/characters/style/threads/卷主线一次出齐）→ 写回蓝图 + 落 bible；
- **volume**：单卷主线细化 → `outline/volumes.json`（卷闸门：只展开 vol=1 的 chapter）；
- **chapter**：章节细纲 → `outline/chapters/<vol>-<ch>.md`（front-matter + 行内
  `key_events:`/`出场人物:` 双通道——`parse_key_events`/`parse_cast_decl` 都是行内 regex）。

节点协议（LLM 输出）：`{"artifact": {...}|null, "decide": "done|expand", "reason": "...",
"children": [...], "open_questions": [...]}`。**decide=expand 由引擎驱动递归**：
volume→arc、chapter→beat（F4b/F4c）、worldview/character_group→子层（F4 DFS）——
叶节点（style/thread_set/arc/beat/character/setting_entry）的 prompt 不再要求
decide/children（H11：要求了也被确定性丢弃，白耗 token）。

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
    extra_instruction: str = ""    # 审核 revise：用户修改建议追加进 user prompt
    arcs: list[dict] | None = None  # 本卷 arc 产物（chapter 节点 prompt 注入，F4b）
    extra: dict = field(default_factory=dict)  # roll 注入的四块上下文（§7.7）等
    extra_warnings: list[str] = field(default_factory=list)
    reject_note: str = ""   # 方案4：章纲去重闸拒绝原因（重生成 prompt 附带，成功后清空）


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
  "threads": [{"id": "pt:xxx", "desc": "伏笔内容", "scope": "book|volume", "target_vol": 1, "carrier": "回收后若载体将持续出场可填 object|goal|character|emotion|faction|theme，否则省略"}],
  "lines": [{"id": "ln:xxx", "desc": "线索内容一句话", "kind": "main|subplot|hidden",
             "carrier": "object|goal|character|emotion|faction|theme", "scope": "book|volume",
             "members": ["char:xxx"], "target": {"vol": 1, "note": "远期落点一句话"}}],
  "volumes": [{"vol": 1, "title": "卷名", "summary": "本卷主线", "key_beats": ["关键转折"],
               "threads_to_payoff": ["pt:xxx"]}],
  "time_origin": "故事时间原点（t=0 锚点）"
}"""


# ---- D13：brief 硬约束锚点（不得改动项，全 forge prompt 注入） ----
def _anchors_block(ctx: NodeContext) -> str:
    anchors = ctx.bp.get("meta.anchors") or []
    if not anchors:
        return ""
    return "\n".join(f"- {a}" for a in anchors[:12])


# ---- 2026-09-06 商讨轮扩展：pace/romance/opening 三维度（用户拍板进规划层）----
_PACE_VOLUME_HINT = {
    "平推爽文": "升级/碾压反馈密集，outcome 多为进取得胜；但每卷 cost 仍须真实代价，防无敌流审美疲劳。",
    "苟住发育": "主角前期避战蓄力，前两卷 outcome 允许「守住既有成果即胜」（护住所 build 的东西），大胜与扬眉吐气留到后段。",
    "先抑后扬": "每卷先压后扬：低谷放在本卷 30%-40% 处，outcome 呈「先失后得」结构。",
    "稳健推进": "按剧情自然推进，outcome 允许得胜/受挫/惨胜，保持张力即可。",
}
_ROMANCE_RULE = {
    "无CP": "全书不得生成感情戏、暧昧、婚约情节；女性角色出场仅按剧情职能。",
    "单女主": "感情线集中于单一对象，不得新增暧昧对象；感情戏占比克制（每卷至多两三处点缀）。",
    "多女主后宫": "多名女性角色并行发展，关系不提前收敛为单一对象；各线进度均衡，不得长期遗忘某一对象。",
    "副线淡化": "感情内容仅作点缀，不得占据事件主线；无告白/婚约级推进。",
}
_OPENING_RULE = {
    "开局即冲突": "前三章每章须有一个显性冲突/危机事件，交代信息借冲突带出，不安排纯铺垫章。",
    "金手指速觉醒": "金手指须在第 3 章结束前觉醒并完成一次核心机制展示。",
    "慢热铺垫": "前三章以世界观浸润与处境铺垫为主，允许无强冲突，但每章仍须有章末钩子。",
}


def _pace_section(meta: dict) -> str:
    """卷纲注入：节奏模式 → 卷弧 outcome 语义提示（纯函数，无 IO）。"""
    hint = _PACE_VOLUME_HINT.get(str(meta.get("pace") or ""))
    return f"\n\n【节奏模式（用户拍板：{meta['pace']}）】{hint}" if hint else ""


def _chapter_meta_rules(meta: dict, ch: int) -> str:
    """细纲注入：开篇节奏（前三章）+ 感情线模式 cast 约束（纯函数，无 IO）。"""
    out = ""
    if ch <= 3 and _OPENING_RULE.get(str(meta.get("opening") or "")):
        out += (f"\n开篇节奏（用户拍板：{meta['opening']}）："
                f"{_OPENING_RULE[meta['opening']]}")
    if _ROMANCE_RULE.get(str(meta.get("romance") or "")):
        out += (f"\n感情线模式（用户拍板：{meta['romance']}，角色卡与事件生成都须遵守）："
                f"{_ROMANCE_RULE[meta['romance']]}")
    return out


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
    anchors = _anchors_block(ctx)
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
    if anchors:
        fixed["硬性锚点（用户原话，一字不得改写，时间跨度/数值/年龄均以此为准）"] = anchors
    # F5 修复（2026-09-05）：商讨轮 meta.endgame 此前无任何消费者（死数据流）——
    # 用户拍板的结局走向必须进 book/volume prompt，卷规划才收敛于既定结局。
    if meta.get("endgame"):
        fixed["结局走向（用户拍板，volumes 规划须收敛于此）"] = meta.get("endgame")
    # 2026-09-06：商讨轮新增维度（pace/romance/opening）——同 endgame 死数据流教训，
    # 用户拍板必须进 book prompt，骨架规划才有消费者。
    if meta.get("pace"):
        fixed["节奏模式（用户拍板，升级/打脸密度与卷弧 outcome 据此定）"] = meta["pace"]
    if meta.get("romance"):
        fixed["感情线模式（用户拍板，角色阵容与感情戏占比据此定）"] = meta["romance"]
    if meta.get("opening"):
        fixed["开篇节奏（用户拍板，前三章据此起笔）"] = meta["opening"]
    user = f"""你是网文立项设定师。为一部新书产出完整设定骨架。只输出 JSON，不要任何解释。

【创意提炼 / 已确定项（不得改动，须原样保留）】
{json.dumps(fixed, ensure_ascii=False, indent=2)}

{pkg_block}

【要求】
1. 主角必给一张卡（role=protagonist）；原话没给主角名就按卖点拟一个合理的。
2. volumes 恰好 {scale.get('volumes', 3)} 卷（vol=1..N），每卷给 title/summary/key_beats/threads_to_payoff。
3. worldview.power_system.levels 沿用模板境界体系，可按本书微调。
4. 所有 id 用前缀：char:/loc:/item:/pt:/ln:。
5. key_beats 每卷 3–6 条；threads 给 3–8 条主线伏笔。
6. 世界观 rules（世界铁律）2–5 条，必须与力量机制自洽。
7. hidden_level 只给"表面修为与实际战力不符"的人物（扮猪吃虎型主角等）；普通人卡省略该键。
8. lines 线索骨架 2–6 条：**主线（kind=main）恰好 1 条**且必须给 target（远期落点）；
   支线 subplot 1–4 条（跨章串联剧情的才算——1-2 章就完结的微线不要登记）；
   暗线 hidden 0–2 条；每条给 carrier（载体类型）与 members（关联实体 id）。

按以下 JSON 输出（键名严格一致，缺省用空对象/空数组）：
{_BOOK_OUTPUT_PROTOCOL}"""
    return "你是网文立项设定师（Forge book 节点，docs/10 §7.2）。", user


def _lines_ledger(ctx: NodeContext) -> list[dict]:
    """线索账本读取（构建期）：bible/lines.json 优先（运行态真实），退回蓝图骨架段。"""
    from ..core.lines import load_lines

    try:
        rows = load_lines(ctx.ws, ctx.project_id)
    except Exception:  # noqa: BLE001 - 降级为现状行为（无账本）
        rows = []
    if rows:
        return rows
    return [r for r in ctx.bp.section("lines") if isinstance(r, dict) and r.get("id")]


def _lines_block_for_volume(ctx: NodeContext, vol: int) -> str:
    """卷纲注入块（四级视图之一）：dormant 骨架全集 + 前卷延续 active 线 + 前卷闭合线 yield。"""
    rows = _lines_ledger(ctx)
    parts: list[str] = []
    # 2026-09-06 用户拍板：感情线模式联动线索账本——单女主/后宫时建议登记为一条
    # subplot 线，纳入冷却/检查点管束（防感情线写到一半蒸发）。账本为空也提示。
    romance = ""
    try:
        romance = str(ctx.bp.get("meta.romance") or "") if ctx.bp is not None else ""
    except Exception:      # noqa: BLE001 - 蓝图缺 meta.romance（旧蓝图）→ 无建议
        romance = ""
    if romance in ("单女主", "多女主后宫"):
        has_rom = any("romance" in str(r.get("id") or "")
                      or "感情" in str(r.get("desc") or "") for r in rows)
        if not has_rom:
            parts.append("感情线登记建议（用户已拍板感情线模式：" + romance
                         + "）：账本尚无感情线，建议 line_plan.open 登记 ln:romance"
                         "（kind=subplot，desc 写明对象与关系走向），纳入冷却与"
                         "检查点管束，防感情线中道蒸发。")
    if not rows:
        return "\n".join(parts)
    dormant = [r for r in rows if r.get("status") in ("dormant", "pending")]
    if dormant:
        parts.append("待开线索骨架（登记未开，本卷可按计划开启）：")
        parts.extend(f"- {r['id']}（{r.get('kind')}）：{str(r.get('desc') or '')[:40]}"
                     for r in dormant[:8])
    active = [r for r in rows if r.get("status") in ("active", "suspended")]
    if active:
        parts.append("前卷延续的在途线索（本卷必须继续推进或显式收束）：")
        parts.extend(f"- {r['id']}（{r.get('kind')}，{r.get('status')}）：{str(r.get('desc') or '')[:40]}"
                     for r in active[:6])
    closed = [r for r in rows if (r.get("status") == "closed" or r.get("closed"))
              and r.get("yield")]
    if closed:
        parts.append("已闭合线索的收获（支线 yield 回流主线的落点，本卷消费或呼应）：")
        parts.extend(f"- {r['id']} → {str(r.get('yield'))[:60]}" for r in closed[:6])
    due = [r for r in rows if r.get("due")]
    if due:
        parts.append("上卷检查点/卷末审计强制处理项（本卷必须落实，不得再拖）：")
        parts.extend(f"- {r['id']}：{str(r.get('due'))[:70]}" for r in due[:6])
    return "\n".join(parts)


def _volume_prompt(ctx: NodeContext) -> tuple[str, str]:
    bp = ctx.bp
    meta = bp.get("meta") or {}
    scale = meta.get("scale") or {}
    vol = ctx.vol
    plan = next((v for v in bp.section("volumes") if v.get("vol") == vol), None)
    plan_block = json.dumps(plan, ensure_ascii=False, indent=2) if plan else "（无 book 规划，自行拟定）"
    arc = (ctx.pack or {}).get("volume_arc_hint") or "（无模板卷弧提示）"
    anchors_block = _anchors_block(ctx)
    anchor_section = (f"\n\n【硬性锚点（用户原话，本卷主线不得与之矛盾或偷改数值）】\n{anchors_block}"
                      if anchors_block else "")
    # F5：结局走向约束（末卷必须兑现；非末卷须朝它推进）
    endgame = (bp.get("meta") or {}).get("endgame")
    endgame_section = ""
    if endgame:
        is_final = vol >= int(scale.get("volumes", 1) or 1)
        endgame_section = (f"\n\n【结局走向（用户拍板）：{endgame}】\n"
                           + ("本卷为末卷——主线必须在本卷收敛到该结局，"
                              "不得再开新主线钩子。" if is_final else
                              "本卷主线须朝该结局实质推进，不得偏离。"))
    lines_block = _lines_block_for_volume(ctx, vol)
    lines_section = f"\n\n【线索账本视图（ADR-025，卷级）】\n{lines_block}\n" \
                    "本卷 line_plan.open 只能从上列待开线索选 id，不得自造。" \
        if lines_block else ""
    # 批2：卷末审计写回运行态伏笔的 due（到期未回收）→ 本卷卷纲强制处理
    threads_due_section = ""
    try:
        from ..core.lines import _load_threads
        _tdue = [t for t in _load_threads(ctx.ws, ctx.project_id) if t.get("due")]
        if _tdue:
            threads_due_section = (
                "\n\n【伏笔到期强制项（卷末审计）】下列伏笔已到期未回收，"
                "threads_to_payoff 必须包含它们（确需改期请在 chapter_notes 说明）：\n"
                + "\n".join(f"- {t['id']}：{str(t['due'])[:60]}" for t in _tdue[:6]))
    except Exception:  # noqa: BLE001 - 到期项读取失败降级为无强制项
        threads_due_section = ""
    pace_section = _pace_section(meta)
    user = f"""你是卷大纲师。为第 {vol} 卷写出主线（全书 {scale.get('volumes', '?')} 卷 × {scale.get('chapters_per_volume', '?')} 章）。

【本卷规划（book 产物）】{plan_block}

【流派卷弧提示】{arc}{anchor_section}{endgame_section}{pace_section}{lines_section}{threads_due_section}

【输出 JSON】
{{
  "vol": {vol},
  "title": "卷名",
  "summary": "本卷主线一句话（每章生成时注入）",
  "arc": {{"goal": "本卷主线节目标", "obstacle": "核心阻碍", "outcome": "达成或失败（允许受挫，不必全胜）", "cost": "结果与代价（赢了失去什么/输了保住什么）", "bridge": "衔接下一主线节"}},
  "key_beats": ["3-6 个关键转折（含代价事件——胜利/失败都要留账）"],
  "threads_to_payoff": ["本卷须回收的伏笔 id，没有则空数组"],
  "line_plan": {{"open": ["ln:xxx 本卷开启的线索"], "note": "开线计划一句话（活跃支线至多 3 条，超出请显式挂起最冷的线）"}},
  "chapter_notes": "本卷各章走向提示（供细纲师），两三句话"
}}
只输出 JSON。"""
    rc = ctx.extra.get("roll_context")  # forge roll：§7.7 四块上下文
    if rc:
        user += f"\n\n【滚动上下文（前卷事实，优先于规划）】\n{rc}\n以上为第 {vol - 1} 卷已发生的实然，本卷细纲必须承接。"
    return f"你是卷大纲师（Forge volume 节点，第 {vol} 卷，docs/10 §7.2）。", user


# ---- 方案4（质量加固 2026-09-05）：章纲事件母题去重 ----
def _event_key(s: str) -> str:
    """事件句归一：去空白与标点，留纯语义字符（用于包含/重合判定）。"""
    return re.sub(r"[\s，,。；;：:、！？!?（）()【】\[\]\"'“”·—…-]", "", str(s or ""))


def event_dup_violations(key_events: list[str], ledger: list[str], *,
                         jaccard_threshold: float = 0.5,
                         min_jaccard_chars: int = 12) -> list[str]:
    """新细纲事件 vs 已规划账本的重复判定（方案4 确定性闸）。

    归一后整条包含，或字符 Jaccard > `jaccard_threshold` 视为重复。
    Jaccard 只对归一后 ≥`min_jaccard_chars` 的句子生效——短句（"事件1-1"类
    占位/编号句）字符重合天然虚高，交由包含判定即可。ch4/ch5 实证：细纲
    逐字相同时正文会以同条件采样产出近逐字句子——必须在细纲层拦下。
    """
    led_norm = [(_event_key(e)) for e in (ledger or [])]
    led_norm = [x for x in led_norm if x]
    out: list[str] = []
    for ev in key_events or []:
        en = _event_key(ev)
        if not en:
            continue
        eset = set(en)
        for ln in led_norm:
            if en in ln or ln in en:
                out.append(ev)
                break
            if len(en) < min_jaccard_chars or len(ln) < min_jaccard_chars:
                continue
            inter = len(eset & set(ln))
            union = len(eset | set(ln))
            if union and inter / union > jaccard_threshold:
                out.append(ev)
                break
    return out


def planned_events_ledger(bp: Blueprint, vol: int, ch: int, *,
                          max_events: int = 40) -> list[str]:
    """全卷已规划事件账本（方案4）：本卷 1..ch-1 章 + 上一卷最后 2 章的 key_events。

    数据源 = 蓝图 chapters 段（build 期每章 apply 后即入账；resume 从盘上载入），
    无需引擎额外维护状态。上一卷只取尾部 2 章（防跨卷开头复读，控制块大小）。
    """
    events: list[str] = []
    scale = ((bp.get("meta") or {}).get("scale") or {})
    K = int(scale.get("chapters_per_volume", 0)) or 0
    for g in bp.section("chapters"):
        try:
            gv, gc = int(g.get("vol") or 0), int(g.get("ch") or 0)
        except (TypeError, ValueError):
            continue
        same_vol = gv == vol and 0 < gc < ch
        prev_vol = gv == vol - 1 and K > 0 and gc > K - 2  # 上一卷最后 2 章
        if not (same_vol or prev_vol):
            continue
        for e in g.get("key_events") or []:
            s = str(e).strip()
            if s and s not in events:
                events.append(s)
    return events[-max_events:]


def planned_titles(bp: Blueprint, vol: int, ch: int, *, n: int = 2) -> list[str]:
    """近 n 章标题（同卷优先，不足回退上一卷尾）——标题句式复读检查用（方案4/问题1）。"""
    rows = sorted((g for g in bp.section("chapters")
                   if int(g.get("vol") or 0) == vol and 0 < int(g.get("ch") or 0) < ch),
                  key=lambda g: int(g.get("ch") or 0))
    titles = [str(g.get("title") or "") for g in rows[-n:]]
    if len([t for t in titles if t]) < n:
        prev = sorted((g for g in bp.section("chapters")
                       if int(g.get("vol") or 0) == vol - 1),
                      key=lambda g: int(g.get("ch") or 0))
        titles = [str(g.get("title") or "") for g in prev[-n:]] + titles
    return [t for t in titles if t][-n:]


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
        {k: c.get(k) for k in ("id", "name", "role", "gender", "core_traits", "flaw", "power") if c.get(k)}
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
    threads_bp = [t for t in bp.section("threads") if t.get("id")]
    threads_block = "；".join(
        f"{t['id']}（{str(t.get('desc') or '')[:24]}）" for t in threads_bp[:12]) or "（本书暂无伏笔）"
    # ADR-025：章纲层线索视图（唯一决策层）——active 全量单行卡 + 冷却告警置顶 +
    # closed 禁复活负清单；lines_present 动作由本节点产出并过人审（事实开启点）。
    from ..core.lines import chapter_view
    from ..core.phase import Phase, PhasePolicy, VolumeContext

    lines_block = ""
    lines_rule = ""
    try:
        _policy = PhasePolicy.load(ctx.ws, ctx.project_id)
        _vctx = VolumeContext.load(ctx.ws, ctx.project_id, vol)
        _phase, _why = _policy.judge(_vctx, ch)
        ledger = _lines_ledger(ctx)
        if ledger:
            lines_block, _lwarns = chapter_view(
                ledger, vol, ch, int(scale.get("chapters_per_volume", 20) or 20),
                tail_phase=_phase is Phase.TAIL, opening_phase=_phase is Phase.OPENING)
            if lines_block:
                lines_rule = ("\n\n" + lines_block
                              + "\n\n9. lines_present：为上列每条 active 线给一个本章动作"
                                "（advance 推进 / flicker 露头（主线休眠章合法形态）/"
                                "suspend 显式挂起）；dormant 线按计划 open；动作目标写在 note。"
                                "已闭合线不得出现在 lines_present。")
    except Exception:  # noqa: BLE001 - 线索视图任何异常降级为无注入
        lines_block = ""
    anchors_block = _anchors_block(ctx)
    anchor_section = (f"\n\n【硬性锚点（用户原话，细纲不得与之矛盾或偷改数值/时间跨度）】\n{anchors_block}"
                      if anchors_block else "")
    # D15：开篇 beat——第一章细纲必须包含世界/主角交代点
    opening_rule = ("6. 全书第一章：key_events 须含一个开场交代事件（借冲突带出世界现状、"
                    "力量体系与主角身份处境），并让本章出场人物完成亮相。" if (vol, ch) == (1, 1) else "")
    # 方案4：全卷已规划事件账本 + 拒绝重生成说明 + 标题句式禁复读
    ledger_events = planned_events_ledger(bp, vol, ch)
    recent_titles = planned_titles(bp, vol, ch)
    ledger_block = ""
    if ledger_events:
        ledger_block = ("\n\n【已规划事件账本（此前章节已分配的事件——key_events 严禁复用或高度相似）】\n"
                        + "\n".join(f"- {e[:60]}" for e in ledger_events[-24:]))
    reject_block = ""
    if ctx.reject_note:
        reject_block = ("\n\n【上一稿被拒】以下事件与账本重复：" + ctx.reject_note
                        + "\n必须产出**全新**的事件（新冲突/新场景/新推进），违者整稿作废。")
    title_rule = ""
    if recent_titles:
        title_rule = (f"\n8. 本章标题不得与近期标题（{'、'.join(recent_titles)}）"
                      "同句式，禁止套用同一标题模板（如『XX重构修仙』复读）。")
    # G4 修复（2026-09-05）：连读审查 findings 注入**下一章细纲**——原先只进
    # 正文侧（orchestrator goal），细纲层发现的母题重复照样固化进账本。
    bans = [str(b) for b in ((ctx.extra or {}).get("coherence_bans") or []) if b]
    bans_block = ""
    if bans:
        bans_block = ("\n\n【连读审查禁令（此前章细纲连读发现的问题，本章细纲必须规避）】\n"
                      + "\n".join(f"- {b}" for b in bans[:8]))
    dedup_rule = ("7. key_events 严禁与【已规划事件账本】中任何条目重复或高度相似"
                  "（同主角+同动作+同对象即视为重复）；「与前一章衔接」指因果承接，"
                  "不是重复叙述同一事件。")
    meta_rules = _chapter_meta_rules(meta, ch)
    user = f"""你是细纲师。写第 {vol} 卷第 {ch} 章的章节细纲（全书 {scale.get('chapters_per_volume', '?')} 章/卷）。

【本卷主线】{vol_block}

【本章所属章段弧】{arc_line}

【前一章（因果连续，必须衔接）】
{prev_block}
{actual_block}{anchor_section}{ledger_block}{reject_block}{bans_block}
【本章可用角色卡】
{char_block}

【节奏提示】{json.dumps(rhythm, ensure_ascii=False)}

【纪律】
1. key_events 恰好 2–3 个（每章事件数上限），每个一句话、可执行、含动作与结果。
2. after_days：相对上一事件的天数（连续推进填 0；有明确间隔填天数，如 3）。
3. characters 用角色 id（char:xxx），只列本章实际出场者。
4. threads_involved 只能从本书伏笔清单选 id：{threads_block}；本章没碰就空数组，禁止自造 id。
5. turns 1–3 条：本章转折/推进点。
6. tension：一句话说清本章张力来源（主角的两难/威胁/悬念——每个事件都要服务于它，
   不是重复事件内容）。hook：章末钩子（最后一个事件以此收尾，拉住读者翻下一章）。
{opening_rule}{dedup_rule}{title_rule}{meta_rules}{lines_rule}

【输出 JSON】
{{
  "title": "本章标题（不带'第 N 章'）",
  "pov": "视角（默认：第三人称限知（主角视角））",
  "key_events": ["事件1", "事件2"],
  "turns": ["转折/推进1"],
  "tension": "本章张力来源一句话（主角在两难什么）",
  "hook": "章末钩子一句话",
  "characters": ["char:xxx"],
  "threads_involved": ["伏笔清单中的 pt:xxx"],
  "lines_present": [{{"id": "ln:xxx", "action": "open|advance|suspend|flicker|close", "note": "本章这条线做什么（一句话）"}}],
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


def _protocol_block(fields: str, *, leaf: bool = False) -> str:
    """输出协议（H11 修复，2026-09-05）：叶节点（style/thread_set/arc/beat/
    character/setting_entry）不再要求 decide/reason/children——引擎对叶节点的
    decide/children 确定性丢弃，要求模型产出只是浪费输出 token，且若模型把
    实质内容只写进 children 会静默丢失。
    """
    if leaf:
        return f"""【输出 JSON】
{{
{fields}}}
- 只输出 JSON。"""
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

{_protocol_block('  "artifact": {"id": "set:xxx", "keywords": ["词1", "词2"], "text": "设定正文（一段话，含数值/边界等硬细节）"},', leaf=True)}"""
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

{_protocol_block('  "artifact": { …完整人物卡… },', leaf=True)}"""
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
不改 pov/tense（已与商讨答案对齐）。{_craft_block(st)}

{_protocol_block('  "artifact": { …完整文风… },', leaf=True)}"""
    return "你是文风定稿师（Forge style 节点，docs/10 §7.1 L1）。", user


def _craft_block(st: dict) -> str:
    """题材工艺卡约束（2026-09-05 真机教训：风格解释权全交给模型 → 模型把
    "系统提示音/叮/机械音"当爽文俗套加进禁用词表，导致系统流小说里系统零次发声）。

    风格节点必须知晓本书已勾选的工艺规范，且不得生成与之冲突的禁用词。
    """
    ids = [str(x) for x in (st.get("craft_cards") or []) if str(x).strip()]
    if not ids:
        return ""
    from ..craft.loader import inject_block

    blk = inject_block(ids)
    if not blk:
        return ""
    return (
        "\n\n【本书已启用的题材工艺卡（硬约束，文风不得与之冲突）】\n"
        f"{blk}\n"
        "注意：上列卡片明确规定的呈现标识（如【】专用于系统发言）"
        "及其相关提示音/机械音色，属于题材核心要素，**不得写入 forbidden_words**；"
        "若嫌俗套，应规范其呈现频次与格式，而不是禁用。"
    )


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

{_protocol_block('  "artifact": {"threads": [ …伏笔清单… ]},', leaf=True)}"""
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

{_protocol_block('  "artifact": { …本弧产物… },', leaf=True)}"""
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

{_protocol_block('  "artifact": {"beats": ["…"]},', leaf=True)}"""
    return "你是重场戏节拍师（Forge beat 节点，docs/10 §7.1 L4）。", user


# ---- apply（落盘 + 蓝图回写，provenance 保护）----
def _coerce_str_list(v):
    """LLM 数组字段防御归一（schema 要求 string 数组）。

    强模型常把"分类列表"输出成 dict（如 civilizations={社会结构:…, 科技:…}）
    或单串——schema 校验在 bp.save 时才跑，直接崩掉整次构建（2026-09-04 云端
    Qwen3.6-35B 真机实证）。规则：dict → "键：值" 列表；str → 单元素；
    list 内非 str 项展开/字符串化；归不出非空列表返回 None（调用方跳过）。
    """
    if isinstance(v, dict):
        return [f"{k}：{val}" for k, val in v.items()
                if isinstance(val, str) and val.strip()]
    if isinstance(v, str):
        return [v.strip()] if v.strip() else None
    if isinstance(v, list):
        out: list[str] = []
        for item in v:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                out.extend(f"{k}：{val}" for k, val in item.items()
                           if isinstance(val, str) and val.strip())
            elif item:
                out.append(str(item))
        return out or None
    return None


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
        if k in ("rules", "factions", "civilizations"):
            cv = _coerce_str_list(v)
            if not cv:
                continue
            if bp.is_protected(f"worldview.{k}"):
                continue
            wv[k] = cv
            bp.set_provenance(f"worldview.{k}", "llm", 0.8)
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
        if k in ("tone", "forbidden_words"):
            # schema 要求 string 数组；强模型常给 dict 分类或单串（2026-09-04 真机实证）
            cv = _coerce_str_list(v)
            if not cv:
                continue
            if bp.is_protected(f"style.{k}"):
                continue
            st[k] = cv
            bp.set_provenance(f"style.{k}", "llm", 0.8)
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


# ---- 方案7（质量加固 2026-09-05）：角色卡 schema 防污染 ----
_GENERIC_ROLE_IDS = {"char:protagonist": "protagonist", "char:rival": "rival",
                     "char:love_interest": "love_interest", "char:mentor": "mentor"}


def _coerce_character_card(card: dict) -> tuple[dict, list[str]]:
    """角色卡 name 防污染归一（方案7）：括号注记挪入 background；超长名截取。

    真机实证（proj-cloud5）：LLM 把整段人设写进 name（“苏晚晴（青梅竹马，前世主角
    最愧疚之人…）”40 字）→ 广播精确匹配失败误杀（B2 三级放大的上游根）。
    返回 (card, warns)。name 缺失原样返回。
    """
    warns: list[str] = []
    raw = str(card.get("name") or "").strip()
    if not raw:
        return card, warns
    name = raw
    m = re.search(r"[（(【\[]", raw)
    if m:
        name = raw[:m.start()].strip(" ，,、；;。")
        note = raw[m.start():].strip("（）()【】[] ，,、；;。")
        if note:
            bg = str(card.get("background") or "").strip()
            card["background"] = (note + ("；" + bg if bg else ""))[:200]
    if len(name) > 12:
        cut = re.split(r"[，,；;：:。！？是的]", name)[0].strip()
        bg = str(card.get("background") or "").strip()
        card["background"] = (f"名字原注：{name}" + ("；" + bg if bg else ""))[:200]
        name = cut if 1 < len(cut) <= 12 else name[:4]
        warns.append(f"角色名超长已归一：{raw[:24]}… → {name}")
    if name:
        card["name"] = name
    return card, warns


def _norm_card_key(s: str) -> str:
    return re.sub(r"\s+", "", str(s or ""))


def _merge_generic_cards(bp: Blueprint) -> list[str]:
    """泛型 id 卡（char:protagonist 等）与实名卡同角色/同名 → 并入实名卡（方案7）。

    真机实证：seed 骨架卡（char:li_tianjie）与 book 节点产出的泛型卡
    （char:protagonist）并存 → 实体双记（两个 key 各 104 次提及），别名共指失效。
    在 book 落盘时合并（章细纲尚未生成、无 id 引用，删除安全）。
    """
    warns: list[str] = []
    for gid, role in _GENERIC_ROLE_IDS.items():
        g = bp.find_by_id("characters", gid)
        if not isinstance(g, dict):
            continue
        gname = _norm_card_key(g.get("name"))
        twin = None
        for c in bp.section("characters"):
            cid = str(c.get("id") or "")
            if not cid or cid == gid:
                continue
            aliases = {_norm_card_key(a) for a in (c.get("aliases") or [])}
            if c.get("role") == role or (gname and _norm_card_key(c.get("name")) == gname) \
                    or (gname and gname in aliases):
                twin = c
                break
        if twin is None:
            continue
        for k in ("background", "core_traits", "power", "arc", "gender",
                  "aliases", "hook", "relationships", "first_appear"):
            v = g.get(k)
            if v and not twin.get(k):
                twin[k] = v
        if not twin.get("role"):
            twin["role"] = role
        bp.data["characters"] = [c for c in bp.section("characters")
                                 if c.get("id") != gid]
        warns.append(f"泛型卡 {gid} 与实名卡 {twin.get('id')} 同角色/同名，已合并")
    return warns


_RELATION_TEMPLATES = ("青梅竹马", "未婚妻", "指腹为婚", "娃娃亲", "救命恩人",
                       "宿敌", "死对头", "金手指持有者", "重生者", "穿书者")


def _character_conflict_warnings(bp: Blueprint) -> list[str]:
    """卡间人设模板冲突检查（方案7/B1）：同一关系模板被多卡使用 → 警告。

    真机实证：林婉清卡与苏晚晴卡同采“青梅竹马”模板 → 卡间冲突、正文称谓漂移。
    """
    hits: dict[str, list[str]] = {}
    for c in bp.section("characters"):
        blob = " ".join([str(c.get("background") or "")]
                        + [str(t) for t in (c.get("core_traits") or [])])
        for t in _RELATION_TEMPLATES:
            if t in blob:
                hits.setdefault(t, []).append(str(c.get("name") or c.get("id")))
    return [f"人设模板「{t}」被多卡使用（{'、'.join(names)}）——人物关系可能撞型，请核对"
            for t, names in hits.items() if len(names) > 1]


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

    # characters（含主角兜底；方案7：落库前 name 防污染归一 + 泛型卡合并 + 撞型检查）
    chars_in = []
    for c in art.get("characters") or []:
        if isinstance(c, dict) and c.get("id") and c.get("name"):
            c, w = _coerce_character_card(c)
            warns.extend(w)
            chars_in.append(c)
    _apply_items(bp, "characters", chars_in)
    warns.extend(_merge_generic_cards(bp))
    warns.extend(_character_conflict_warnings(bp))
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

    # lines 线索骨架（ADR-025）：dormant 登记进蓝图 lines 段（sync_bible 导出 bible/lines.json）；
    # 主线唯一 = 硬校验（raise → 引擎重试），main 缺 target 自动补占位 + 告警。
    warns.extend(_apply_lines_skeleton(bp, art.get("lines") or []))

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


def _apply_lines_skeleton(bp: Blueprint, rows: list) -> list[str]:
    """book 产物 lines 骨架登记（ADR-025 阶段2）。

    蓝图 lines 段只存**应然骨架**（id/desc/kind/carrier/scope/members/target，
    status 恒 dormant——实然由 sync_bible 合并盘上运行态）。硬校验：
    - 主线不唯一 → ValueError（引擎重试，与事件去重闸同一模式）；
    - main 缺 target → 自动补占位（末卷落点）+ 告警（不阻断）。
    """
    from ..core.lines import new_line, validate_lines

    warns: list[str] = []
    scale = bp.get("meta.scale") or {}
    skeleton: list[dict] = []
    for r in rows or []:
        if not isinstance(r, dict) or not str(r.get("id") or "").strip() \
                or not str(r.get("desc") or "").strip():
            continue
        lid = str(r["id"]).strip()
        if not lid.startswith("ln:"):
            tail = re.sub(r"^(ln|line)[:：]", "", lid).strip(":： ") or "line"
            lid = f"ln:{tail}"
        row = new_line(lid, str(r["desc"]),
                       kind=str(r.get("kind") or "subplot"),
                       carrier=str(r.get("carrier") or ""),
                       scope=str(r.get("scope") or "book"),
                       members=[str(m) for m in (r.get("members") or []) if m],
                       target=r.get("target") if isinstance(r.get("target"), dict) else None)
        skeleton.append(row)
    mains = [x for x in skeleton if x["kind"] == "main"]
    if len(mains) == 0 and skeleton:
        warns.append("lines 骨架缺主线（kind=main 恰好 1 条），请人工补登")
    for x in mains:
        if not (isinstance(x.get("target"), dict) and x["target"].get("vol")):
            x["target"] = {"vol": int(scale.get("volumes", 1) or 1),
                           "note": "（book 节点未给远期落点，占位待人工修订）"}
            warns.append(f"{x['id']}: main 缺 target，已按末卷占位")
    for row in skeleton:
        existing = bp.find_by_id("lines", row["id"])
        if existing is None:
            bp.upsert("lines", dict(row))
            bp.set_provenance(f"lines[{row['id']}]", "llm", 0.8)
        else:
            merged = dict(existing)
            for k, v in row.items():
                if not bp.is_protected(f"lines[{row['id']}].{k}"):
                    merged[k] = v
            bp.upsert("lines", merged)
    # 主线唯一硬校验作用在**合并后的账本**上（骨架 + 已有行）——重跑 build 追加
    # 第二条主线同样要拦（引擎重试）。
    merged_errs = validate_lines(bp.section("lines"))
    hard = [e for e in merged_errs if "主线不唯一" in e]
    if hard:
        raise ValueError(f"{hard[0]}，拒绝落盘并重生成——主线唯一是硬约束")
    warns.extend(e for e in merged_errs if "主线不唯一" not in e)
    return warns


def _apply_threads(bp: Blueprint, threads: list) -> None:
    for t in threads or []:
        if not isinstance(t, dict) or not t.get("id") or not t.get("desc"):
            continue
        tid = t["id"]
        t.setdefault("scope", "book")
        t.setdefault("status", "unplanned")
        # carrier（批2·ADR-025）：伏笔载体（object/goal/character/...）——回收后
        # 满足跨章判据时由卷末审计提名升级为线（"令牌回收后持续出场→物线索"）。
        if t.get("carrier") and str(t["carrier"]) not in (
                "object", "goal", "character", "emotion", "faction", "theme"):
            t["carrier"] = "object"
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
    warns: list[str] = []
    scale = bp.get("meta.scale") or {}
    K = int(scale.get("chapters_per_volume", 20))
    vol = ctx.vol
    # 卷弧五元组（ADR-025 阶段2）：目标/阻碍/达成或失败/结果含代价/衔接——outcome 允许
    # 受挫，治"每卷必全胜"的旧问题 C1。归一为 dict[str,str]，缺键留空。
    arc_raw = art.get("arc") if isinstance(art.get("arc"), dict) else {}
    arc = {k: str(arc_raw.get(k) or "").strip()[:120]
           for k in ("goal", "obstacle", "outcome", "cost", "bridge")}
    if any(arc.values()):
        warns.append(f"卷 {vol}：五元组 outcome='{arc['outcome'][:30]}'"
                     + ("" if arc["outcome"] else "（未给）")
                     + "——允许受挫，key_beats 应含代价事件")
    # 本卷开线计划（line_plan）：登记进卷行供细纲师消费；悬空 id 丢弃 + 告警
    plan_raw = art.get("line_plan") if isinstance(art.get("line_plan"), dict) else {}
    valid_ln = {str(r.get("id")) for r in _lines_ledger(ctx)}
    open_ids = []
    for x in _str_list(plan_raw.get("open")):
        if x not in valid_ln:
            warns.append(f"卷 {vol}：line_plan.open 悬空线索 {x!r} 丢弃")
            continue
        open_ids.append(x)
    line_plan = {"open": open_ids, "note": str(plan_raw.get("note") or "")[:120]}
    # 活跃支线预算（告警级）：账本 active 支线 > 3 → 提示挂起最冷线
    from ..core.lines import active_lines

    sub_active = [r for r in active_lines(_lines_ledger(ctx)) if r.get("kind") == "subplot"]
    if len(sub_active) > 3:
        warns.append(f"卷 {vol}：活跃支线 {len(sub_active)} 条 > 预算 3（{'、'.join(r['id'] for r in sub_active)}）"
                     "——请显式挂起最冷的线")
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
    if any(arc.values()):
        row["arc"] = arc
    if open_ids or line_plan["note"]:
        row["line_plan"] = line_plan
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
        "tension": str(gist.get("tension") or ""),   # D1：张力来源（行内注入事件 prompt）
        "hook": str(gist.get("hook") or ""),         # D1：章末钩子
        "characters": [str(c) for c in (gist.get("characters") or [])],
        "threads_involved": [str(t) for t in (gist.get("threads_involved") or [])],
        "lines_present": [dict(x) for x in (gist.get("lines_present") or [])
                          if isinstance(x, dict)],   # ADR-025：本章线索动作（事件层消费）
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
    if fm.get("tension"):
        # D1：行内注入（本 md 整篇进章级 goal → 每个事件的 prompt 都看得见）
        parts.append(f"- 全章张力（每个事件都要服务于它，不得偏离）：{fm['tension']}")
    if fm.get("hook"):
        # A3 归因修正（2026-09-05）：钩子行随整篇 md 注入**每个**事件——旧文案
        # "最后一个事件必须以此收尾"诱导前置事件抢跑写钩子（ch1 两半重演根因之一）。
        parts.append(f"- 章末钩子（收束方向指引：仅最后一个事件以此收尾；"
                     f"前面的事件只须让情节朝此方向发展，严禁提前写钩子内容）：{fm['hook']}")
    if fm["threads_involved"]:
        parts.append(f"- 伏笔：{'、'.join(fm['threads_involved'])}")
    if fm["lines_present"]:
        # ADR-025：行内 JSON（core/lines.parse_line_decl 解析，事件层命中注入的声明源）
        parts.append(f"本章线索: {json.dumps(fm['lines_present'], ensure_ascii=False)}")
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
    # D4：threads_involved 对齐 blueprint 实际伏笔——悬空 id 丢弃（模型自造语义名
    # 如 pt:jade_talisman 与 thread_set 产出的 pt:N 体系不一致，V2 会 block）
    valid_tids = {t.get("id") for t in bp.section("threads") if t.get("id")}
    threads = []
    for t in (art.get("threads_involved") or []):
        ts = str(t)
        if not ts.startswith(("pt:", "thread:")):
            continue
        if ts not in valid_tids:
            warns.append(f"chapter {vol}-{ch}: 悬空伏笔引用丢弃 {ts!r}")
            continue
        threads.append(ts)
    key_events = _str_list(art.get("key_events"))[:6]
    if not key_events:
        if not art:
            warns.append(f"chapter {vol}-{ch}: 模型产物为空（裸 JSON 未含业务键或空回复），"
                         f"落盘占位细纲——请检查上游 prompt/解析")
        else:
            warns.append(f"chapter {vol}-{ch}: key_events 为空，用占位事件兜底")
        key_events = [f"第 {ch} 章主线推进"]
    # 方案4：章纲事件母题去重闸——首稿与账本重复 → 拒绝落盘并触发引擎重试
    #（reject_note 附入重生成 prompt）；重试仍重复 → 按 ADR-017 retry=1 降级接受+告警。
    try:
        _viol = event_dup_violations(key_events, planned_events_ledger(bp, vol, ch))
    except Exception:  # noqa: BLE001 - 闸门自身异常不阻断细纲
        _viol = []
    if _viol and not ctx.reject_note:
        ctx.reject_note = "；".join(v[:40] for v in _viol[:3])
        raise ValueError(f"chapter {vol}-{ch}: key_events 与已规划事件重复"
                         f"（{ctx.reject_note}），拒绝落盘并重生成")
    if _viol:
        warns.append(f"chapter {vol}-{ch}: 重生成后仍与账本相似"
                     f"（{_viol[0][:40]}…），按 ADR-017 retry=1 降级接受")
    else:
        ctx.reject_note = ""
    gist = {
        "vol": vol,
        "ch": ch,
        "title": str(art.get("title") or f"第 {ch} 章"),
        "pov": str(art.get("pov") or "") or "第三人称限知（主角视角）",
        "key_events": key_events,
        "turns": _str_list(art.get("turns"))[:6],
        # D1 情节工艺（2026-09-04 拍板）：张力来源 + 章末钩子。校验从宽——
        # 旧细纲/模型没给就留空字符串，只少注入两行，绝不 block。
        "tension": str(art.get("tension") or "").strip()[:120],
        "hook": str(art.get("hook") or "").strip()[:80],
        "characters": cids,
        "threads_involved": threads,
        "after_days": int(art.get("after_days") or 0),
    }
    # ADR-025 阶段2：lines_present 人审落定 → 账本动作（open/advance/suspend/flicker/close）
    # + 确定性校验（悬空 id / 死线复活 / 收尾期禁 open / 开篇期 hidden 禁揭开 = 告警不阻断）。
    # 声明先随 fm/细纲 md 落盘（事件层消费），再落账本；落账失败不影响细纲。
    lp_warns: list[str] = []
    acts: list[dict] = []
    for x in art.get("lines_present") or []:
        if isinstance(x, dict) and str(x.get("id") or "").startswith("ln:"):
            acts.append({"id": str(x["id"]), "action": str(x.get("action") or "advance"),
                         "note": str(x.get("note") or "")[:80]})
    if acts:
        gist["lines_present"] = acts
    md = render_gist_md(gist, vol, ch, names)
    p = ctx.ws.outline_chapter_path(ctx.project_id, vol, ch)
    p.parent.mkdir(parents=True, exist_ok=True)
    ctx.ws.write_text(p, md)
    _upsert_chapter_bp(bp, gist)

    if acts:
        try:
            from ..core.lines import apply_chapter_actions
            from ..core.phase import Phase, PhasePolicy, VolumeContext

            _policy = PhasePolicy.load(ctx.ws, ctx.project_id)
            _vctx = VolumeContext.load(ctx.ws, ctx.project_id, vol)
            _phase, _ = _policy.judge(_vctx, ch)
            ledger = _lines_ledger(ctx)
            if not ledger:
                lp_warns.append(f"chapter {vol}-{ch}: lines_present 有声明但账本为空，全部忽略")
            else:
                lp_warns.extend(apply_chapter_actions(
                    ctx.ws, ctx.project_id, ledger, vol, ch, acts,
                    tail_phase=_phase is Phase.TAIL,
                    opening_phase=_phase is Phase.OPENING))
        except Exception as e:  # noqa: BLE001 - 线索落账失败不阻断细纲（ADR-021 降级纪律）
            lp_warns.append(f"chapter {vol}-{ch}: lines_present 落账失败"
                            f"（{type(e).__name__}），细纲已正常落盘")
    warns.extend(lp_warns)
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


def _node_out_tokens(kind: str, bp: Any) -> int:
    """节点输出 token 预算。

    book 节点的 artifact 含全部卷规划，随 meta.scale.volumes 线性放大——
    3 卷蓝图在 2600 预算下 JSON 残缺（2026-09-05 真机：连续两次解析失败回退）。
    其余节点输出规模与卷数无关，维持 2600。
    """
    base = 2600
    if kind == "book":
        try:
            n = int(((bp.get("meta.scale") or {}).get("volumes")) or 1)
        except Exception:  # noqa: BLE001
            n = 1
        return base + 900 * max(0, n - 1)
    return base


def run_node(ctx: NodeContext, kind: str) -> NodeResult:
    """执行一个节点：prompt → LLM → 解析（协议）→ apply。抛 ValueError = 解析失败（引擎重试）。"""
    if kind not in _PROMPTS:
        raise ValueError(f"unknown node kind: {kind}")
    node_id = _node_id_of(kind, ctx)
    system, user = _ensure_json_hint(*_PROMPTS[kind](ctx))
    if ctx.extra_instruction:
        user += f"\n\n【用户修改建议（本轮重生成须落实）】\n{ctx.extra_instruction}"
    res = ctx.provider.complete(LLMRequest(
        messages=[LLMMessage(role="system", content=system),
                  LLMMessage(role="user", content=user)],
        temperature=0.5, max_tokens_out=_node_out_tokens(kind, ctx.bp),
        response_format="json_object",
        thinking=False))  # 生成类：蓝图节点生成，关思考
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


# ---- 审核 revise：book 模块定向段重生成（ADR-024）----
def revise_book_section(provider: Any, bp: Blueprint, module: str,
                        suggestions: str) -> tuple[Any, list[str]]:
    """按用户建议重生成单个 book 审核模块（一次 LLM 调用，只动该模块段）。

    返回（新段内容, 与旧版的确定性 diff）。不落盘——调用方负责 bp.save/sync_bible。
    """
    from .review import REVIEW_MODULES, diff_section

    spec = REVIEW_MODULES.get(module)
    if not spec or spec["kind"] != "book":
        raise ValueError(f"not a book-section module: {module}")
    sections = spec["sections"]
    old = {sec: bp.data.get(sec) for sec in sections}
    meta = bp.get("meta") or {}
    # 兄弟模块要点（保持连贯，不整体重生成）
    sibling = {
        "title": meta.get("title"), "genre": meta.get("genre"),
        "logline": meta.get("logline"), "scale": meta.get("scale"),
        "characters": [c.get("name") for c in bp.data.get("characters") or []],
        "threads": [f"{t.get('id')}:{str(t.get('desc'))[:30]}"
                    for t in bp.data.get("threads") or []],
    }
    cur_json = json.dumps(old, ensure_ascii=False, indent=2)
    user = f"""你是网文设定修订师。只输出 JSON 对象，不要任何解释。

【全书固定项（不得改动）】
{json.dumps(sibling, ensure_ascii=False, indent=2)}

【当前「{spec['label']}」模块内容】
{cur_json}

【用户修改建议（必须落实）】
{suggestions}

请输出该模块的新版 JSON：顶层键固定为 {json.dumps(sections, ensure_ascii=False)}，
各键的值结构与「当前模块内容」完全一致。未涉及建议的部分尽量原样保留。"""
    system = "你是网文设定修订师。严格按用户建议修订设定，只输出 JSON。"
    system, user = _ensure_json_hint(system, user)
    new: dict = {}
    from ..core.llm import ModerationBlockedError

    _REVISE_RETRIES = 2   # flash 生成抖动：偶发 content 空/畸形 JSON，重试收敛（经验同 broadcast B1）
    for _ in range(_REVISE_RETRIES + 1):
        res = provider.complete(LLMRequest(
            messages=[LLMMessage(role="system", content=system),
                      LLMMessage(role="user", content=user)],
            temperature=0.4, max_tokens_out=3200, response_format="json_object",
            thinking=False))  # 生成类：单模块修订重生成，关思考
        if res.blocked:
            raise ModerationBlockedError(res.block_reason, res.provider_note)
        try:
            maybe = json.loads(res.content or "")
        except (ValueError, TypeError):
            maybe = {}
        if isinstance(maybe, dict) and any(sec in maybe for sec in sections):
            new = maybe
            break
        new = {}
    merged = {}
    for sec in sections:
        if sec in new:
            # G6 修复（2026-09-05）：整段替换改为**保护合并**——与正常落库路径
            # _apply_items 同一契约（docs/10 §7.6 user 字段永不被模型产物覆盖）。
            bp.data[sec] = _merge_protected_section(bp, sec, bp.data.get(sec), new[sec])
            merged[sec] = bp.data[sec]  # diff 以实际合并结果为准（保护字段不算变更）
    if not merged:
        raise ValueError(f"revise reply missing section keys {sections}")
    bp.data["rev"] = int(bp.data.get("rev") or 1) + 1
    diffs: list[str] = []
    for sec in sections:
        diffs.extend(diff_section(old.get(sec), merged.get(sec), sec))
    return merged, diffs


# ---- 确定性落盘（零 LLM）----

def _merge_protected_section(bp: Blueprint, sec: str, old, new):
    """整段修订的保护合并（G6 修复，2026-09-05）。

    - dict 段：逐键合并，`bp.is_protected(f"{sec}.{k}")` 的键保留旧值；
    - list 段：按 id upsert 逐字段保护；新段未提及的旧条目**保留**（修订只动
      建议相关条目，防误删——保守优先于灵活）；
    - 结构不匹配/空旧值：退回直接替换（无从保护）。
    """
    if isinstance(old, dict) and isinstance(new, dict):
        out = dict(old)
        for k, v in new.items():
            if bp.is_protected(f"{sec}.{k}"):
                continue
            out[k] = v
        return out
    if isinstance(old, list) and isinstance(new, list):
        old_items = {x.get("id"): dict(x) for x in old
                     if isinstance(x, dict) and x.get("id")}
        out: list = []
        for item in new:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            iid = item["id"]
            if iid in old_items:
                m = old_items[iid]
                for k, v in item.items():
                    if not bp.is_protected(f"{sec}[{iid}].{k}"):
                        m[k] = v
                out.append(m)
            else:
                out.append(dict(item))
        for iid, x in old_items.items():
            if all(it.get("id") != iid for it in out):
                out.append(x)
        return out
    return new


# 运行态字段白名单：这些字段由生成/回写组件维护（交代状态机 verify、编纂员伏笔流转、
# worldstate 状态域），蓝图里没有真实值。sync_bible 可能晚于生成运行（蓝图修订重落盘），
# 全量覆盖会把运行态抹掉（实证 proj-20260903194907：ch1-5 verify 已置 revealed=true，
# 重同步后全被重置回 false → 交代状态机空转）。布尔第二位：是否保留盘上独有行/键
# （True = 工厂/enrich 运行期追加的行与扩展键不被覆盖；False = 盘上仅蓝图行，直接换新）。
_RUNTIME_FIELDS: dict[str, tuple[tuple[str, ...], bool]] = {
    "bible/settings.json": (("revealed", "first_ch"), True),
    "bible/plot_threads.json": (("status", "planted", "returned"), False),
    # 线索账本（ADR-025）：蓝图只持应然骨架（id/desc/kind/carrier/scope/members/target），
    # 其余全是运行态；keep_extra=True 保留盘上独有行（生成期提名 pending、人工转正行）。
    "bible/lines.json": (("status", "opened", "last_seen", "progress", "yield",
                          "closed", "closing_candidate", "resume_hint"), True),
    "bible/items.json": (("state", "aliases"), False),
    "bible/skills.json": (("state", "aliases"), False),
    "bible/locations.json": (("status", "aliases"), True),
    "bible/characters.json": ((), True),
}


def _merge_bible_rows(ws: Workspace, project_id: str, rel: str, rows: list[dict],
                      runtime_fields: tuple[str, ...] = (), keep_extra: bool = False) -> list[dict]:
    """蓝图重写前按 id 合并盘上运行态（ADR-016 文件=事实源）。

    合并方向：蓝图计划字段胜；`runtime_fields` 盘上值胜；`keep_extra` 时盘上独有键
    （provenance/behavior_rules 等 enrich/工厂补喂）与独有行（工厂注册的新卡）保留，
    独有行追加在尾部。蓝图里要**删除**实体请直接改盘（同步是合并不是镜像）。
    """
    p = ws._abs(f"{project_id}/{rel}")  # noqa: SLF001
    try:
        old = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    except (ValueError, OSError):
        old = []
    by_id = ({r.get("id"): r for r in old if isinstance(r, dict) and r.get("id")}
             if isinstance(old, list) else {})
    if not by_id:
        return rows
    out: list[dict] = []
    seen: set = set()
    for row in rows:
        prev = by_id.get(row.get("id"))
        if prev is None:
            out.append(row)
            continue
        seen.add(row.get("id"))
        if keep_extra:
            merged = {k: v for k, v in prev.items() if k not in row}
        else:
            merged = dict(row)
        merged.update(row)
        for f in runtime_fields:
            if prev.get(f) is not None:
                merged[f] = prev[f]
        out.append(merged)
    if keep_extra:
        out.extend(r for rid, r in by_id.items() if rid not in seen)
    return out


def sync_bible(ws: Workspace, project_id: str, bp: Blueprint) -> list[str]:
    """蓝图 → bible 文件落盘（剥离 role、补默认字段、主角引用）。返回写入的相对路径。

    非 mirror 语义：按 id 合并盘上运行态（_RUNTIME_FIELDS / _merge_bible_rows），
    生成期攒下的 revealed/伏笔流转/状态域与工厂/enrich 追加行不被覆盖。
    """
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
    # characters（剥离 role；protagonist → is_protagonist；运行期扩展键/工厂卡保留）
    # 方案7：落盘前 name 防污染归一——存量污染卡（人设长句塞 name）在重同步时被修复，
    # 广播池/实体别名/调度匹配从此拿到干净名字。
    chars = []
    for c in bp.section("characters"):
        c, _cw = _coerce_character_card(dict(c))
        card = {k: v for k, v in c.items() if k != "role"}
        if c.get("role") == "protagonist":
            card["is_protagonist"] = True
        chars.append(card)
    chars = _merge_bible_rows(ws, project_id, "bible/characters.json", chars, (), True)
    for card in chars:
        card.setdefault("status", "active")
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
        # F6 修复（2026-09-05）：thread_set 产出的 plant_vol 此前被白名单丢弃、
        # V6 伏笔密度检查读的 planted.vol 全链路无人写（恒空转）。这里映射为
        # schema 的 planted={vol, ch:0}（ch 未知按 0，回收判定按卷粒度）。
        if row.get("plant_vol") is not None and not row.get("planted"):
            try:
                row["planted"] = {"vol": int(row["plant_vol"]), "ch": 0}
            except (TypeError, ValueError):
                pass
        threads.append({k: v for k, v in row.items()
                        if k in ("id", "desc", "scope", "target_vol", "planted",
                                 "status", "report_deadline", "returned", "revision",
                                 "plant_desc", "payoff_desc")})
    threads = _merge_bible_rows(ws, project_id, "bible/plot_threads.json", threads,
                                *_RUNTIME_FIELDS["bible/plot_threads.json"])
    write("bible/plot_threads.json", threads)
    # lines 线索账本（ADR-025）：应然骨架 + 运行态合并（同 threads 的非 mirror 语义）
    lines_rows = []
    for ln in bp.section("lines"):
        if not isinstance(ln, dict) or not ln.get("id"):
            continue
        row = dict(ln)
        row.setdefault("status", "dormant")
        # 蓝图骨架行可能缺实然键（sync 早于任何回写）——补齐形状，schema 校验不炸
        for k in ("opened", "last_seen", "progress", "yield", "closed",
                  "closing_candidate"):
            row.setdefault(k, None if k != "progress" else [])
        lines_rows.append(row)
    if lines_rows or ws._abs(f"{project_id}/bible/lines.json").exists():  # noqa: SLF001
        lines_rows = _merge_bible_rows(ws, project_id, "bible/lines.json", lines_rows,
                                       *_RUNTIME_FIELDS["bible/lines.json"])
        write("bible/lines.json", lines_rows)
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
            rt_fields, keep_extra = _RUNTIME_FIELDS[rel]
            rows = _merge_bible_rows(ws, project_id, rel, rows, rt_fields, keep_extra)
            if rel == "bible/settings.json":  # 与 synthesize_seed_settings 卡形对齐（自描述）
                for r in rows:
                    r.setdefault("revealed", False)
                    r.setdefault("first_ch", 1)
            write(rel, rows)
    # D2：seed 模式 build 无 settings 节点 → V3（settings ≥5）必挡、检索空转。
    # 零 LLM 从蓝图实体合成种子卡；仅蓝图与磁盘双空时兜底（enrich 增量不被覆盖）。
    if not bp.section("settings") and not ws._abs(f"{project_id}/bible/settings.json").exists():
        write("bible/settings.json", synthesize_seed_settings(bp))
    return written


def _setting_slug(name: str, fallback: str) -> str:
    """实体名 → set id 后缀：ASCII 转小写下划线，中文等不可转字符走 md5 短哈希（确定性）。"""
    import hashlib
    import re as _re

    slug = _re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")
    if not slug:
        slug = hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
    return f"{fallback}_{slug}" or fallback


def synthesize_seed_settings(bp: Blueprint) -> list[dict]:
    """蓝图实体 → 种子设定卡（零 LLM，D2）。

    卡形与 bible/settings.schema 对齐：{id: set:<kind>:<slug>, keywords, text,
    revealed=false, first_ch}。文本只拼接卡上已有字段，**不发明新设定**——
    应然事实仍以 bible 为准；检索库非空后 supplement_settings 才能滚动归类。
    """
    cards: list[dict] = []

    def _card(kind: str, name: str, keywords: list[str], text: str, first_ch: int = 1) -> None:
        text = text.strip("：;；,， ")
        if not name or not text:
            return
        cards.append({"id": f"set:{kind}:{_setting_slug(name, kind)}",
                      "keywords": [k for k in dict.fromkeys([name, *keywords]) if k],
                      "text": text, "revealed": False, "first_ch": first_ch})

    wv = bp.get("worldview") or {}
    ps = _dict_of(wv.get("power_system"))
    parts = []
    if ps.get("mechanic"):
        parts.append(f"力量机制：{ps['mechanic']}")
    if ps.get("levels"):
        parts.append(f"境界体系：{'、'.join(map(str, ps['levels']))}")
    for r in (wv.get("rules") or [])[:3]:
        parts.append(f"铁律：{r}")
    if parts:
        _card("world", str(wv.get("name") or "世界观"), ["境界", "修炼"],
              "；".join(parts))

    for c in bp.section("characters"):
        pw = _dict_of(c.get("power"))
        seg = [str(c.get("background") or "")]
        if pw.get("level"):
            seg.append(f"修为 {pw['level']}" + (f"（{pw['faction']}）" if pw.get("faction") else ""))
        if pw.get("hidden_level"):
            seg.append(f"隐藏实力 {pw['hidden_level']}")
        _card("char", str(c.get("name") or ""),
              [str(a) for a in (c.get("aliases") or [])],
              f"{c.get('name')}：{'；'.join(x for x in seg if x)}",
              int(((c.get("first_appear") or {}).get("ch")) or 1))

    for kind, section in (("loc", "locations"), ("item", "items"), ("skill", "skills")):
        for x in bp.section(section):
            _card(kind, str(x.get("name") or ""),
                  [str(a) for a in (x.get("aliases") or [])],
                  f"{x.get('name')}：{x.get('desc') or x.get('note') or ''}")

    # id 去重（同名跨段极端情况）
    seen: set[str] = set()
    uniq = []
    for card in cards:
        if card["id"] in seen:
            continue
        seen.add(card["id"])
        uniq.append(card)
    return uniq


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
