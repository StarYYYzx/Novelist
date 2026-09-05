"""商讨问答协议（docs/10 §5.3，M3l F2）。

核心原则：**问题由引擎（确定性）决定，候选由模型（LLM）生成**。

流程（`run_consult`）：
1. `detect_gaps` → 缺口槽位（required/recommended 未填或低置信）
2. `group_slots` → 分轮（每轮 ≤4 问，轮次边界 = 保存点）
3. 每轮：LLM 批量生成候选（candidates_from=llm 的槽位，**1 次调用/轮**）
4. 单屏呈现 + 一次输入，`parse_round_line` 纯函数解析三种回答：
   - 回车 → 全取推荐值（src 按候选来源：template/llm）
   - `N xxx` / `N` → 第 N 项自由答案/选编号（src=user, conf=1.0）
   - `q` → 结束问答，剩余取推荐值（transcript 留痕，resume 续问）
5. 答案写蓝图（set_provenance）+ 每轮末保存 + transcript 逐项留痕
6. 非 TTY → 全取推荐值 + warn + transcript（AG3 依赖此行为）

**resume 幂等**：已答槽位 key 从 transcript 的 `ask.answer` 事件读回，自动跳过——
再调 `run_consult` 即从断点继续（无需显式轮次号）。

自由答案：直接采纳（src=user、conf=1.0）；类型不在 Genre Pack 库 → 调用方装载通用包，
并把用户的类型描述注入后续节点 prompt（docs/10 §5.3 末尾，engine 已按 bp 现状生成）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from ..core.llm import LLMMessage, LLMRequest
from ..storage.workspace import Workspace
from .io_console import AnswerIO, ConsoleIO
from .seed import _slug  # noqa: PLC2701 - 名字转 id 片段（seed 已定义，避免复制）
from .slots import Gap, Slot, _resolve_key, default_slots, detect_gaps, group_slots
from .state import Blueprint, append_transcript

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
class RoundAnswer:
    values: dict[str, AnsweredValue]  # slot_key -> 采纳值
    quit: bool = False  # 用户 q 提前退出
    note: str = ""  # 解析告警（H6：序号越界等，供调用方提示——静默丢弃即 bug）


@dataclass
class ConsultResult:
    ok: bool
    answered: int = 0  # 本次采纳槽位数
    free_answers: int = 0  # 其中用户明确给的数量
    rounds_done: int = 0
    quit_early: bool = False
    downgraded: bool = False  # 非 TTY 全取推荐值
    warnings: list[str] = field(default_factory=list)


# ---- 单轮回答解析（纯函数，可测）----

_ROUND_RE = re.compile(r"^(\d+)\s*(.*)$")


def parse_round_line(line: str, qs: list[RoundQuestion]) -> RoundAnswer:
    """解析单行输入。`qs` 为该轮全部问题（顺序即编号）。"""
    line = (line or "").strip()
    if not line:
        return RoundAnswer({q.slot.key: _default_value(q) for q in qs})
    if line.lower() == "q":
        return RoundAnswer({q.slot.key: _default_value(q) for q in qs}, quit=True)
    m = _ROUND_RE.match(line)
    if not m:
        # 无法识别：保守全取推荐值（不把整行当自由答案，避免误写）
        return RoundAnswer({q.slot.key: _default_value(q) for q in qs},
                           note=f"无法识别的输入「{line[:40]}」，已全取推荐值")
    n = int(m.group(1))
    rest = m.group(2).strip()
    if not (1 <= n <= len(qs)):
        # H6 修复（2026-09-05）：序号越界原先静默丢弃用户答案（全部槽位取推荐值
        # 且无任何提示）。现在显式告警，其余槽位仍取推荐值。
        return RoundAnswer({q.slot.key: _default_value(q) for q in qs},
                           note=f"序号 {n} 超出本轮题数 {len(qs)}，"
                                f"输入「{line[:40]}」未被采纳，已全取推荐值")
    values: dict[str, AnsweredValue] = {}
    for i, q in enumerate(qs, 1):
        if i == n:
            # 用户明确表态：选编号（取该候选）或 "N xxx" 自由答案
            value = rest if rest else (q.candidates[n - 1] if n - 1 < len(q.candidates) else q.default)
            values[q.slot.key] = AnsweredValue(value, "user", explicit=True)
        else:
            values[q.slot.key] = _default_value(q)
    return RoundAnswer(values)


def _default_value(q: RoundQuestion) -> AnsweredValue:
    src = "template" if q.slot.candidates_from in ("template", "enum") else "llm"
    return AnsweredValue(q.default, src, explicit=False)


# ---- 候选生成（每轮 1 次 LLM 调用）----

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
            temperature=0.5, max_tokens_out=700, response_format="json_object"))
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
    """组装该轮问题。`need_candidates=False`（非 TTY）时跳过 LLM 候选。"""
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

def _render_round(round_no: int, label: str, qs: list[RoundQuestion]) -> str:
    bar = "─" * 22
    lines = [f"── 第 {round_no} 轮：{label} {bar}"]
    for i, q in enumerate(qs, 1):
        cand_text = "  ".join(f"({j}) {c[:40]}" for j, c in enumerate(q.candidates, 1))
        lines.append(f"[{i}] {q.slot.ask or q.slot.label}？  {cand_text}")
    lines.append(bar)
    lines.append('回车=全用推荐值 | 输入 "N xxx" 改某一项（xxx 可为自由答案） | q=结束（剩余用推荐值）')
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
        terms = style.setdefault("glossary", [])
        if not any(t.get("term") == value for t in terms):
            terms.append({"term": value})
        return f"style.glossary[term:{value[:20]}]"
    if key in _LIST_TEXT_KEYS or isinstance(bp.get(key), list):
        bp.set(key, _to_text_list(value))
    else:
        bp.set(key, value)
    bp.set_provenance(key, src, conf)
    return key


# ---- 主流程 ----

def _answered_keys(ws: Workspace, project_id: str) -> set[str]:
    """transcript 里已答槽位 key 集合（resume 续问判据）。"""
    from .state import read_transcript

    keys: set[str] = set()
    for ev in read_transcript(ws, project_id):
        if ev.get("event") == "ask.answer" and ev.get("key"):
            keys.add(str(ev["key"]))
    return keys


def run_consult(ws: Workspace, project_id: str, bp: Blueprint, *,
                provider, io: AnswerIO | None = None,
                slots: list[Slot] | None = None) -> ConsultResult:
    """商讨问答（docs/10 §5.3）。答案落蓝图并保存；调用方随后负责构建。

    resume 幂等：已答槽位自动跳过（transcript `ask.answer` 判据）。
    """
    io = io or ConsoleIO()
    warnings: list[str] = []
    gaps = detect_gaps(bp, slots)
    if not gaps:
        return ConsultResult(ok=True, answered=0, rounds_done=0)
    rounds = group_slots([g.slot for g in gaps])
    answered_keys = _answered_keys(ws, project_id)
    result = ConsultResult(ok=True, downgraded=not io.is_tty)
    tty = io.is_tty

    for round_no, (label, round_slots) in enumerate(rounds, 1):
        todo = [s for s in round_slots if s.key not in answered_keys]
        if not todo:
            continue
        append_transcript(ws, project_id, "round.start", round=round_no, label=label)
        if not tty:
            warnings.append(f"非交互环境：第 {round_no} 轮全取推荐值")
            append_transcript(ws, project_id, "round.downgrade", round=round_no)
        qs = _build_questions(bp, todo, provider, round_no, ws, project_id,
                              warnings, need_candidates=tty)
        if tty:
            io.notify(_render_round(round_no, label, qs))
            line = io.ask_free("", "")
            if line is None:  # ConsoleIO 对 "q" 返回 None（退出信号）
                ans = RoundAnswer({q.slot.key: _default_value(q) for q in qs}, quit=True)
            else:
                ans = parse_round_line(line, qs)
        else:
            ans = RoundAnswer({q.slot.key: _default_value(q) for q in qs})
        if ans.note:  # H6：解析告警显式提示 + 留痕，不再静默
            io.notify(f"[提示] {ans.note}")
            warnings.append(ans.note)
        for key, av in ans.values.items():
            slot = next(s for s in todo if s.key == key)
            path = _apply_slot_value(bp, slot, av.value, av.src, 1.0 if av.explicit else 0.8)
            if path is None:
                append_transcript(ws, project_id, "ask.skip", key=key, value=av.value[:80])
                continue
            append_transcript(ws, project_id, "ask.answer", key=key, path=path,
                              value=av.value[:200], src=av.src, explicit=av.explicit)
            result.answered += 1
            if av.explicit:
                result.free_answers += 1
        bp.save(ws, project_id)  # 轮次边界 = 保存点（中断可续）
        result.rounds_done += 1
        if ans.quit:
            result.quit_early = True
            append_transcript(ws, project_id, "round.quit", round=round_no)
            break
    result.warnings = warnings
    return result
