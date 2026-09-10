"""商讨问答协议（docs/10 §5.3，M3l F2；2026-09-10 改为 guided 对话式）。

核心原则：**问哪些由引擎（确定性缺口）决定，听懂用户自由语由 LLM 负责，
写回与校验始终由确定性层背书**。

流程（`run_consult`）：
1. `detect_gaps` → 缺口槽位（required/recommended 未填或低置信），按优先级排序
2. 每轮取**最优先缺口 1~3 个**提问（候选由 LLM 生成，仅作提示）
3. 用户**自由回答**（可混编多题答案 + 补充设想，格式随意）
4. 单次 LLM 分派：自由语 → 本轮 allowed_keys 的槽位↔值映射 + extras（无关设想）
5. **确定性护栏**：enum/索引消歧 + 值合法预检；非法值**拒绝不入库**（重新追问或
   两轮后强制取推荐值）——绝不让非法值进 blueprint（修复 2026-09-10 schema 崩溃）
6. 写蓝图（set_provenance=user）+ 每轮末保存 + transcript 逐项留痕
7. `?`/`@show`：打印已填设定概览（含 provenance/缺口/extras）后回到同一轮
8. extras：分派中无槽可归的补充设想 → `workspace/forge/extras.json` 登记待确认
   （不进 bible，不自动写回，provenance=user）
9. 非 TTY → 全取推荐值 + warn + transcript（AG3 依赖此行为）

**resume 幂等**：已答槽位 key 从 transcript 的 `ask.answer` 事件读回，自动跳过。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from ..core.llm import LLMMessage, LLMRequest
from ..storage.workspace import Workspace
from .io_console import AnswerIO, ConsoleIO
from .seed import _slug  # noqa: PLC2701 - 名字转 id 片段（seed 已定义，避免复制）
from .slots import Slot, _resolve_key, default_slots, detect_gaps
from .state import Blueprint, append_transcript

EXTRAS_FILE = "extras.json"
_MAX_WINDOW = 3  # 每轮最多问几个优先缺口（诉诸自然对话体量）
_RETRY_LIMIT = 2  # 同一槽位连续非法拒答轮次，超过则强制取推荐值并告警


# ---- 回答数据结构 ----

@dataclass
class AnsweredValue:
    value: str
    src: str  # user | template | llm
    explicit: bool  # 用户明确给（自由答案或选编号）


@dataclass
class RoundQuestion:
    slot: Slot
    candidates: list[str]
    default: str


@dataclass
class ConsultResult:
    ok: bool
    answered: int = 0  # 本次采纳槽位数
    free_answers: int = 0  # 其中用户明确给的数量
    rounds_done: int = 0
    quit_early: bool = False
    downgraded: bool = False  # 非 TTY 全取推荐值
    warnings: list[str] = field(default_factory=list)
    extras_seen: int = 0  # 本次登记的补充设想条数


# ---- 候选生成（每轮 1 次 LLM 调用，仅作提示）----

CANDIDATE_SYSTEM = """你是网文设定编辑。为下列每个设定问题给出 2-4 个候选答案。
候选要贴合本书已有设定，与已有内容自洽，不要空泛套话。
只输出 JSON，不要任何解释或前后缀。"""


def _blueprint_context(bp: Blueprint, max_chars: int = 400) -> str:
    """蓝图已定设定摘要（候选生成的上下文，防候选与已有设定冲突）。"""
    meta = bp.get("meta") or {}
    wv = bp.get("worldview") or {}
    st = bp.get("style") or {}
    parts = [
        f"类型：{meta.get('genre', '—')}（模板：{meta.get('template', '—')}）",
        f"卖点：{meta.get('logline', '—')}",
    ]
    if wv.get("power_system", {}).get("mechanic"):
        parts.append(f"金手指：{wv['power_system']['mechanic']}")
    if wv.get("power_system", {}).get("levels"):
        parts.append(f"境界：{'、'.join(wv['power_system']['levels'])}")
    if wv.get("name"):
        parts.append(f"世界名：{wv['name']}")
    if st.get("tone"):
        parts.append(f"基调：{'、'.join(st['tone'])}")
    text = "\n".join(parts)
    return text[:max_chars]


def _candidates_prompt(bp: Blueprint, slots: list[Slot], round_no: int) -> str:
    qlines = "\n".join(f'  "{s.key}": "{s.ask or s.label}"' for s in slots)
    return f"""本书已有设定：
{_blueprint_context(bp)}

请为第 {round_no} 轮这些设定问题各给出 2-4 个候选：
{qlines}

只输出 JSON，形如 {{"<问题key>": ["候选1", "候选2"], ...}}。"""  # noqa: RUF027


def _parse_candidates(raw: str) -> dict:
    text = raw.strip()
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        return {}
    out = {}
    for k, v in data.items():
        if isinstance(v, list):
            out[k] = [str(x).strip() for x in v if str(x).strip()][:4]
        elif isinstance(v, str) and v.strip():
            out[k] = [v.strip()][:4]
    return out


def _gen_candidates(bp: Blueprint, slots: list[Slot], provider, round_no: int,
                    ws: Workspace, project_id: str, warnings: list[str]) -> dict:
    """LLM 批量生成候选；失败返回 {}（该项按默认值兜底）。"""
    try:
        res = provider.complete(LLMRequest(
            messages=[LLMMessage(role="system", content=CANDIDATE_SYSTEM),
                      LLMMessage(role="user", content=_candidates_prompt(bp, slots, round_no))],
            temperature=0.5, max_tokens_out=700, response_format="json_object",
            thinking=False))  # 生成类：商讨候选生成，关思考
        if res.blocked:
            raise RuntimeError(f"审核拦截: {res.block_reason or 'unknown'}")
        if not (res.content or "").strip():
            raise RuntimeError("候选生成返回空内容")
        cands = _parse_candidates(res.content)
        if not cands:
            raise RuntimeError("候选解析为空")
        return cands
    except Exception as e:  # noqa: BLE001 - 候选失败不阻断商讨（docs/10 §5.3 兜底）
        warnings.append(f"候选生成失败（第 {round_no} 轮），该项按默认值: {e}")
        append_transcript(ws, project_id, "candidates.fallback", round=round_no, error=str(e)[:200])
        return {}


def _current_text(bp: Blueprint, slot: Slot) -> str:
    v = _resolve_key(bp, slot.key)
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return "、".join(str(x.get("term") if isinstance(x, dict) else x) for x in v)
    return str(v)


def _template_candidates(bp: Blueprint, slot: Slot) -> list[str]:
    """template 类槽位：候选 = 蓝图现值/包默认（本质是确认性质）。"""
    val = _current_text(bp, slot) or slot.default
    return [val] if val else []


def _build_questions(bp: Blueprint, slots: list[Slot], provider,
                     round_no: int, ws: Workspace, project_id: str,
                     warnings: list[str], need_candidates: bool) -> list[RoundQuestion]:
    """组装每轮的问题与候选。`need_candidates=False`（非 TTY）时跳过 LLM 候选。"""
    llm_slots = [s for s in slots if s.candidates_from == "llm"]
    llm_cands: dict = {}
    if llm_slots and need_candidates:
        llm_cands = _gen_candidates(bp, llm_slots, provider, round_no, ws, project_id, warnings)
    qs: list[RoundQuestion] = []
    for s in slots:
        if s.candidates_from == "enum":
            cands = list(s.enum or [])
        elif s.candidates_from == "template":
            cands = _template_candidates(bp, s)
        else:  # llm
            cands = llm_cands.get(s.key) or []
        default = s.default or _current_text(bp, s) or (cands[0] if cands else "")
        cands = [c for c in dict.fromkeys([x for x in cands if x] + [default]) if c]
        if not cands:
            cands = [default] if default else ["（留空，稍后补）"]
        qs.append(RoundQuestion(s, cands, default))
    return qs


# ---- 单屏呈现 ----

def _display_cand(q: RoundQuestion, cand: str) -> str:
    """候选显示：enum 值带中文名（如卡 id→中文名）则显示「中文名 (id)」。"""
    lbl = (q.slot.enum_labels or {}).get(cand)
    if not lbl or lbl == cand:
        return cand
    return f"{lbl} ({cand})"


def _render_round(round_no: int, qs: list[RoundQuestion]) -> str:
    bar = "─" * 22
    lines = [f"── 第 {round_no} 轮 {bar}"]
    for i, q in enumerate(qs, 1):
        lines.append(f"· Q{i} {q.slot.ask or q.slot.label}？  [{q.slot.key}]")
        if q.candidates:
            lines.append(f"    {q.slot.label} 可选（序号 ｜ 值）：")
            for j, c in enumerate(q.candidates, 1):
                lines.append(f"      ({j}) {_display_cand(q, c)}")
    lines.append(bar)
    lines.append("可一次答多题（如：Q1.2;Q2=自由发挥;Q3=…）＋补充设想 | 回车=取推荐值 | "
                 "?=查看已填 | @show=同上 | q=结束")
    return "\n".join(lines)


def _fmt_value(bp: Blueprint, slot: Slot) -> str:
    v = _resolve_key(bp, slot.key)
    if v is None:
        return ""
    if isinstance(v, list):
        return "、".join(str(x.get("term") if isinstance(x, dict) else x) for x in v)
    return str(v)


def _render_bp_overview(bp: Blueprint, slots: list[Slot]) -> str:
    """已填/待补概览（`?`/`@show` 命令触发，读完回到同一轮不打断）。"""

    gap_keys: set[str] = {g.slot.key for g in detect_gaps(bp, slots)}
    lines = ["── 已填充设定 ──"]
    shown = 0
    for s in slots:
        if s.key in gap_keys:
            continue
        val = _fmt_value(bp, s)
        if not val:
            continue
        prov = bp.get_provenance(s.key) or {}
        lines.append(f"  [{s.key}] {val}  ← {prov.get('src', '?')} "
                     f"(conf={prov.get('confidence', '?')})")
        shown += 1
    if not shown:
        lines.append("  （尚无用户确认的设定，以上为蓝图默认/LLM 建议）")
    waits = [f"  · {k}" for k in sorted(gap_keys)]
    lines.append("── 仍待补 ──" + (("\n" + "\n".join(waits)) if waits else "\n  （已全部满足）"))
    return "\n".join(lines)


# ---- 值写入蓝图（含结构化目标）----

_LIST_TEXT_KEYS = {"worldview.factions", "worldview.rules", "worldview.power_system.levels",
                   "style.forbidden_words", "style.tone", "threads"}


def _to_text_list(text: str) -> list[str]:
    parts = re.split(r"[、，,；;/\n]+", text)
    return [p.strip() for p in parts if p.strip()]


def _apply_slot_value(bp: Blueprint, slot: Slot, value: str, src: str, conf: float) -> str | None:
    """写值 + provenance，返回实际写入路径；空值/占位值跳过返回 None（保持缺口）。"""
    key = slot.key
    if not value or value.startswith("（留空"):
        return None
    if slot.kind == "confirm":
        # 确认类槽位（如 meta.scale 是 dict）不重写值，只留痕——避免字符串覆盖结构
        return f"{key}（确认，值不变）"
    if key.startswith("characters[role:"):
        role = key[len("characters[role:") :].split("]")[0]
        sub = key.split("]", 1)[1].lstrip(".")
        found = next((c for c in bp.section("characters") if c.get("role") == role), None)
        cid = found["id"] if found else f"char:{_slug(value or role, role)}"
        if not found:
            found = {"id": cid, "name": value or "", "role": role, "status": "active"}
            bp.section("characters").append(found)
        # 伪路径不可走 bp.set（_split_key 会当顶层键），直接改 section 内对象
        # 名字槽防污染归一（真机实证：商讨把整段设定写进 rival name → 广播/实体
        # 按短名匹配失败、首登场检查静默跳过）：短名入 name，注记挪 background。
        if sub == "name":
            from .textnorm import coerce_character_name

            clean, note = coerce_character_name(value)
            if clean:
                found["name"] = clean
                if note:
                    bg = str(found.get("background") or "").strip()
                    found["background"] = (note + ("；" + bg if bg else ""))[:200]
            bp.set_provenance(f"characters[{cid}].name", src, conf)
            # H5 修复（2026-09-05）：双写伪路径——detect_gaps 按槽位伪路径
            # （characters[role:*].name）查 provenance，只写实 id 键恒查空，
            # low_confidence 复问对全部人物槽位失效。
            bp.set_provenance(key, src, conf)
            return f"characters[{cid}].name"
        found[sub] = _to_text_list(value) if sub == "core_traits" else value
        bp.set_provenance(f"characters[{cid}].{sub}", src, conf)
        bp.set_provenance(key, src, conf)  # H5：伪路径双写（同上）
        return f"characters[{cid}].{sub}"
    if key == "threads":
        # 结构化：主线伏笔 → {id: pt:* , desc}
        tid = f"pt:{_slug(value, 'thread')}"
        bp.upsert("threads", {"id": tid, "desc": value})
        bp.set_provenance(f"threads[{tid}].desc", src, conf)
        return f"threads[{tid}].desc"
    if key == "style.glossary":
        # 结构化：术语表 → style.glossary[] {term}（无 id，按 term 查重）。
        # 注意不可用 bp.section（只认顶层数组，点路径会创建顶层键）
        style = bp.data.setdefault("style", {})
        terms: list = style.setdefault("glossary", [])
        if not any(t.get("term") == value for t in terms):
            terms.append({"term": value})
        return f"style.glossary[term:{value[:20]}]"
    if key == "style.craft_cards":
        # 结构化：工艺卡是多选列表。蓝图若未预置空列表（非 seed 路径创建的蓝图），
        # 下面的 list 分支会退化成单值写入 → schema 崩（真机 2026-09-05）。
        style = bp.data.setdefault("style", {})
        style["craft_cards"] = _to_text_list(value)
        bp.set_provenance(key, src, conf)
        return "style.craft_cards"
    if key in _LIST_TEXT_KEYS or isinstance(bp.get(key), list):
        bp.set(key, _to_text_list(value))
    else:
        bp.set(key, value)
    bp.set_provenance(key, src, conf)
    return key


# ---- guided：LLM 自由语分派 + 确定性护栏（2026-09-10）----

DISPATCH_SYSTEM = """你是网文立项商讨的整理助手。用户用一句比较随意的话回答本轮数个设定问题，
也可能顺带补充与问题无关的额外设想。把这句话拆解成对指定问题的答案，并收集额外设想。

规则：
- answers 的键**必须取自给定的问题 key**；用户没给到、或内容不确定的问题，不要出现在 answers。
- values 若指向某个候选序号（如“1”、“第二个”），转成对应候选文本；enum 单选槽**只能填其合法候选项之一**。
- 拆不出来、或与任何问题都无关的内容放进 extras（它不会自动写入设定，仅登记待用户确认）。
只输出 JSON，不要任何解释或前后缀。
形如 {"answers": {"meta.romance": "单女主"}, "extras": ["追加设定：女主有隐藏身世"]}。"""


def _dispatch_user_prompt(bp: Blueprint, qs: list[RoundQuestion], line: str) -> str:
    ctx = _blueprint_context(bp)
    qlines = "\n".join(
        f"- [{q.slot.key}] {q.slot.ask or q.slot.label}？"
        + ("  候选：" + "、".join(f"({i}) {c}" for i, c in enumerate(q.candidates, 1))
           if q.candidates else "  （可自由发挥）")
        for q in qs)
    head = "本书已有设定：\n" + ctx + "\n\n本轮问题及其候选：\n" + qlines + "\n\n用户刚才的回答：\n" + line + "\n\n"
    example = '拆解为上面的 JSON 格式：{"answers": {"<问题key>": "答案"}, "extras": ["补充设想"]}'
    return head + example


def _parse_dispatch(raw: str) -> dict | None:
    text = raw.strip()
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    answers = data.get("answers")
    extras = data.get("extras")
    if not isinstance(answers, dict):
        return None
    out = {str(k): str(v) for k, v in answers.items() if isinstance(v, (str, int)) and str(v).strip()}
    extra_list = extras if isinstance(extras, list) else []
    extra_list = [str(x).strip() for x in extra_list if isinstance(x, str) and x.strip()]
    return {"answers": out, "extras": extra_list}


def _llm_dispatch(bp: Blueprint, qs: list[RoundQuestion], line: str, provider,
                  ws: Workspace, project_id: str, round_no: int,
                  warnings: list[str]) -> dict | None:
    """单次 LLM：自由语 → 槽位映射 + extras。失败返回 None（调用方回退取推荐值）。"""
    try:
        res = provider.complete(LLMRequest(
            messages=[LLMMessage(role="system", content=DISPATCH_SYSTEM),
                      LLMMessage(role="user", content=_dispatch_user_prompt(bp, qs, line))],
            temperature=0.2, max_tokens_out=900, response_format="json_object",
            thinking=False))  # 解析类：分派自由语，关思考
        if res.blocked:
            raise RuntimeError(f"审核拦截: {res.block_reason or 'unknown'}")
        if not (res.content or "").strip():
            raise RuntimeError("分派返回空内容")
        parsed = _parse_dispatch(res.content)
        if parsed is None:
            raise RuntimeError("分派解析失败")
        # 只保留本轮 allowed_keys（LLM 不得越到本轮之外写别的槽）
        allowed = {q.slot.key for q in qs}
        parsed["answers"] = {k: v for k, v in parsed["answers"].items() if k in allowed}
        return parsed
    except Exception as e:  # noqa: BLE001 - 分派失败回退取推荐值，不阻塞商讨
        warnings.append(f"分派你那句话时失败（第 {round_no} 轮），本轮改取推荐值: {e}")
        append_transcript(ws, project_id, "dispatch.fallback", round=round_no, error=str(e)[:200])
        return None


def _dispatch_value(q: RoundQuestion, raw: str) -> str | None:
    """确定性护栏：把自由语里的某个值转成合法槽值；非法返回 None（拒答）。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    cands = q.candidates
    if raw.isdigit():  # 数字 → 候选序号（用户可能说“选 2”）
        n = int(raw)
        if 1 <= n <= len(cands):
            return cands[n - 1]
    if q.slot.candidates_from == "enum":  # enum 单选：值必须落在合法候选项
        enum = q.slot.enum or []
        if raw in enum:
            return raw
        # 用户写了中文名 / 「中文名 (id)」→ 反查 id（现 craft_cards 候选即如此展示）
        labels = q.slot.enum_labels or {}
        for cand in enum:
            lbl = labels.get(cand)
            if not lbl:
                continue
            if raw == lbl or raw == f"{lbl} ({cand})" or raw == f"{lbl}({cand})":
                return cand
        return None
    return raw  # 自由文本/模板槽：直接采纳


# ---- extras 登记（不进 blueprint，规避 schema；待用户确认后处置）----

def _extras_path(ws: Workspace, project_id: str):
    from pathlib import Path
    p = ws._abs(f"{project_id}/workspace/forge/{EXTRAS_FILE}")  # noqa: SLF001
    return p if isinstance(p, Path) else Path(str(p))


def _read_extras(ws: Workspace, project_id: str) -> list[dict]:
    try:
        p = _extras_path(ws, project_id)
        if not p.exists():
            return []
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001 - 读取失败当作空，不影响商讨
        return []


def _record_extras(ws: Workspace, project_id: str, ideas: list[str]) -> int:
    if not ideas:
        return 0
    p = _extras_path(ws, project_id)
    exists = p.exists()
    try:
        if exists:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            data = data if isinstance(data, list) else []
        else:
            data = []
    except Exception:  # noqa: BLE001
        data = []
    seen = {x.get("text") for x in data}
    added = 0
    for idea in ideas:
        if idea in seen:
            continue
        data.append({"text": idea, "src": "user", "status": "pending",
                     "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        seen.add(idea)
        added += 1
        append_transcript(ws, project_id, "extra.idea", text=idea[:200])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(p)
    return added


# ---- 主流程 ----

def _answered_keys(ws: Workspace, project_id: str) -> set[str]:
    """transcript 里已答槽位 key 集合（resume 续问判据）。"""
    from .state import read_transcript

    keys: set[str] = set()
    for ev in read_transcript(ws, project_id):
        if ev.get("event") == "ask.answer" and ev.get("key"):
            keys.add(str(ev["key"]))
    return keys


def _default_of(q: RoundQuestion) -> AnsweredValue:
    src = "template" if q.slot.candidates_from in ("template", "enum") else "llm"
    return AnsweredValue(q.default, src, explicit=False)


def run_consult(ws: Workspace, project_id: str, bp: Blueprint, *,
                provider, io: AnswerIO | None = None,
                slots: list[Slot] | None = None,
                allow_llm: bool = True) -> ConsultResult:
    """商讨问答（docs/10 §5.3，guided 对话式）。答案落蓝图并保存；随后构建。

    - 每轮问最优先缺口 1~3 个；自由语由单次 LLM 分派，确定性护栏校验后再写。
    - `?` / `@show` 查看已填概览；`q` 结束（剩余取推荐值）。
    - 非法 enum 值拒答不入库，连续 _RETRY_LIMIT 轮未决则强制取推荐值。
    - `allow_llm=False` 时不做 LLM 分派，一律取推荐值（纯确定性模式）。
    resume 幂等：已答槽位自动跳过（transcript `ask.answer` 判据）。
    """
    io = io or ConsoleIO()
    warnings: list[str] = []
    slots = slots or default_slots()
    gaps = detect_gaps(bp, slots)
    if not gaps:
        return ConsultResult(ok=True, answered=0, rounds_done=0)
    answered_keys = _answered_keys(ws, project_id)
    result = ConsultResult(ok=True, downgraded=not io.is_tty)
    tty = io.is_tty

    if not tty:
        # AG3：非交互环境全取推荐值（一次填完所有缺口）
        warnings.append("非交互环境：全取推荐值，跳过自由语分派")
        append_transcript(ws, project_id, "consult.downgrade")
        for g in gaps:
            q = RoundQuestion(g.slot, [], g.slot.default or _current_text(bp, g.slot) or "")
            av = _default_of(q)
            path = _apply_slot_value(bp, g.slot, av.value, av.src, 0.8)
            if path is None:
                continue
            append_transcript(ws, project_id, "ask.answer", key=g.slot.key, path=path,
                              value=av.value[:200], src=av.src, explicit=False)
            result.answered += 1
        bp.save(ws, project_id)
        result.rounds_done = 1
        result.warnings = warnings
        return result

    attempts: dict[str, int] = {}
    round_no = 0
    while True:
        gaps = detect_gaps(bp, slots)
        todo = [g.slot for g in gaps if g.slot.key not in answered_keys]
        if not todo:
            break
        window = todo[:_MAX_WINDOW]
        round_no += 1
        append_transcript(ws, project_id, "round.start", round=round_no,
                          keys=[s.key for s in window])
        qs = _build_questions(bp, window, provider, round_no, ws, project_id,
                              warnings, need_candidates=tty)
        io.notify(_render_round(round_no, qs))
        line = io.ask_free("", "")
        if line is None or line.strip().lower() == "q":  # q → 结束，剩余取推荐值
            _fill_recommended(bp, qs, ws, project_id, result, answered_keys, "round.quit")
            result.quit_early = True
            append_transcript(ws, project_id, "round.quit", round=round_no)
            break
        cmd = line.strip().lower()
        if cmd in ("?", "@show", "help", "show"):  # 查看已填，回到同一轮
            io.notify(_render_bp_overview(bp, slots) + _extras_banner(ws, project_id))
            continue
        if line.strip() == "":  # 回车 = 本轮通取推荐值并推进
            _settle_round(bp, qs, provider, io, ws, project_id, result,
                          answered_keys, attempts, round_no, "ask.default")
            bp.save(ws, project_id)
            result.rounds_done += 1
            continue

        dispatch = None if not allow_llm else _llm_dispatch(
            bp, qs, line, provider, ws, project_id, round_no, warnings)
        if dispatch is None:
            _settle_round(bp, qs, provider, io, ws, project_id, result,
                          answered_keys, attempts, round_no, "dispatch.fallback")
            bp.save(ws, project_id)
            result.rounds_done += 1
            continue
        for q in qs:
            key = q.slot.key
            raw = dispatch["answers"].get(key)
            if raw is None:
                continue  # 该题没被用户意思涵盖 → 留缺口，后续轮再问
            attempts[key] = attempts.get(key, 0) + 1
            val = _dispatch_value(q, raw)
            if val is None:  # 非法值（如 enum 非候选）→ 拒答
                if attempts[key] >= _RETRY_LIMIT:
                    _fill_one_recommended(bp, q, ws, project_id, result, answered_keys, notes=None)
                    warnings.append(f"「{q.slot.key}」已多次答复未能落到合法值，强制取推荐值")
                else:
                    append_transcript(ws, project_id, "ask.reject", key=key, value=raw[:120],
                                      reason="value not valid")
                    io.notify(f"[提示] 「{q.slot.ask or q.slot.label}」只能取 {q.candidates}，"
                              f"你这句「{raw[:30]}」已保留，稍后可重答。")
                continue
            path = _apply_slot_value(bp, q.slot, val, "user", 1.0)
            if path is None:
                continue
            append_transcript(ws, project_id, "ask.answer", key=key, path=path,
                              value=val[:200], src="user", explicit=True)
            result.answered += 1
            result.free_answers += 1
        added = _record_extras(ws, project_id, dispatch.get("extras", []))
        result.extras_seen += added
        bp.save(ws, project_id)
        result.rounds_done += 1

    result.warnings = warnings
    return result


def _fill_one_recommended(bp: Blueprint, q: RoundQuestion, ws: Workspace,
                          project_id: str, result: ConsultResult, answered_keys: set[str],
                          notes: str | None) -> None:
    """把单个槽位填推荐值并留痕；仅在实际写回时标记为已答。"""
    av = _default_of(q)
    path = _apply_slot_value(bp, q.slot, av.value, av.src, 1.0 if av.explicit else 0.8)
    if path is None:
        return  # 空/占位值（如“（留空，稍后补）”）不写入 → 保持缺口，后续轮继续问
    append_transcript(ws, project_id, "ask.answer", key=q.slot.key, path=path,
                      value=av.value[:200], src=av.src, explicit=av.explicit)
    result.answered += 1
    if av.explicit:
        result.free_answers += 1
    answered_keys.add(q.slot.key)


def _settle_round(bp: Blueprint, window: list[RoundQuestion], provider,
                  io: AnswerIO, ws: Workspace, project_id: str, result: ConsultResult,
                  answered_keys: set[str], attempts: dict[str, int],
                  round_no: int, event: str) -> None:
    """空回车/fallback 通填推荐值；无默认槽不卡死——先跳过（说明），
    连续跳过到 `_RETRY_LIMIT` 后由系统按 LLM 推断补低置信值（待审核）。

    方案2：空回车跳到下一缺口（不无限滞留同槽）。待审核槽注册为
    `src=llm conf=0.4` < threshold → `detect_gaps` 标 low_confidence → 出现在
    `/show` 的"仍待补"，用户可见并审核/修改（2026-09-10）。
    """
    for q in window:
        key = q.slot.key
        if key in answered_keys:
            continue
        av = _default_of(q)
        path = _apply_slot_value(bp, q.slot, av.value, av.src,
                                 1.0 if av.explicit else 0.8)
        if path is not None:
            append_transcript(ws, project_id, "ask.answer", key=key, path=path,
                              value=av.value[:200], src=av.src, explicit=av.explicit)
            result.answered += 1
            if av.explicit:
                result.free_answers += 1
            answered_keys.add(key)
            continue
        # 无默认 → 跳过/系统补设定（防死循环）
        attempts[key] = attempts.get(key, 0) + 1
        if attempts[key] < _RETRY_LIMIT:
            append_transcript(ws, project_id, "ask.defer", key=key, round=round_no)
            io.notify(f"[提示] 「{q.slot.ask or q.slot.label}」暂无默认推荐，"
                      f"本轮回车已跳过。连续跳过将由系统补设定供你审核。")
            continue
        _autoset_slot(bp, q, provider, ws, project_id, result, answered_keys,
                      round_no)
    append_transcript(ws, project_id, event, keys=[q.slot.key for q in window])


def _autoset_slot(bp: Blueprint, q: RoundQuestion, provider, ws: Workspace,
                  project_id: str, result: ConsultResult, answered_keys: set[str],
                  round_no: int) -> None:
    """系统兜底：多次无法落推荐值 → 按 LLM 推断补低置信值（待用户审核）。

    成功写入 conf=0.4（低置信缺口，`/show` 可见待审）；LLM 不可用/仍为空则
    记 `ask.defer` 并把槽移出窗口，避免死循环。"""
    key = q.slot.key
    suggestions = _suggest_slot(bp, q, provider, ws, project_id, round_no, result.warnings)
    val = (suggestions[0] if suggestions else "").strip()
    path = None
    if val:
        path = _apply_slot_value(bp, q.slot, val, "llm", 0.4)
    if path is None:
        append_transcript(ws, project_id, "ask.defer", key=key, round=round_no,
                          reason="no-inference")
        answered_keys.add(key)  # 移出窗口防死循环；保留在 detect_gaps 缺口
        return
    append_transcript(ws, project_id, "ask.auto", key=key, path=path,
                      value=val[:200], src="llm", audit=True)
    answered_keys.add(key)
    result.answered += 1
    result.warnings.append(
        f"「{key}」多次跳过，已按 LLM 推断补「{val}」（低置信，待您审核/修改，"
        f"/show 可见）")


def _suggest_slot(bp: Blueprint, q: RoundQuestion, provider, ws: Workspace,
                  project_id: str, round_no: int, warnings: list[str]) -> list[str]:
    """给单个槽生成候选（系统补设定用，单次 LLM 调用；失败返回空）。"""
    try:
        cands = _gen_candidates(bp, [q.slot], provider, round_no, ws,
                                project_id, warnings)
    except Exception as exc:  # noqa: BLE001 - 单次推断失败不影响主流程
        warnings.append(f"「{q.slot.key}」系统补设定时候选生成失败：{exc}")
        return []
    return cands.get(q.slot.key, []) or []


def _fill_recommended(bp: Blueprint, window: list[RoundQuestion], ws: Workspace,
                      project_id: str, result: ConsultResult, answered_keys: set[str],
                      event: str) -> None:
    """一轮里尚未答的槽位通填推荐值（回车/q/fallback 共用）。"""
    for q in window:
        if q.slot.key in answered_keys:
            continue
        _fill_one_recommended(bp, q, ws, project_id, result, answered_keys, None)
    append_transcript(ws, project_id, event, keys=[q.slot.key for q in window])


def _extras_banner(ws: Workspace, project_id: str) -> str:
    n = len(_read_extras(ws, project_id))
    if not n:
        return "\n（无待确认的补充设想）"
    return f"\n（{n} 条补充设想已登记待确认，`forge extras` 可查看处置）"