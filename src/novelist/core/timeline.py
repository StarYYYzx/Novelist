"""时间轴与定时事件（ADR-019，docs/06 §3.3，M3m T1）。

## 它解决什么

小说里"闭关三月后出关""三年之约"这类**时间承诺**此前完全没有系统表达：
`bible/timeline.json` 有个壳（schema 已定义、checkpoint 已纳入），但零写入方——
proj-t5 实跑 5 章后 `timeline.json` 仍是 `[]`；`LandedEvent.timeline_delta` 字段
（`writeback.py:41`）定义了也无人填。LLM 靠上下文"记得"时间承诺，长卷必漂。

本模块把时间做成**相对天数轴**（开书之日 = 0，不用历法——LLM 维护历法必然漂移）：

- `bible/worldstate.json` 里存 `time.now`（当前时刻）与 `pending[]`（未来日程）；
- `bible/timeline.json` 存历史时点（事实源，ADR-016）；
- 编纂员抽「时间：+90日」「约定：叶蓝出关｜+90日」两行驱动（搭既有抽取调用，零新增 LLM 调用）；
- 临近到期按**分档**注入生成提示（M3m T2），到期不处理 → R-TIME 告警 → 软 block
  → 连续 3 次自动 `expired` 放行（防挂机批跑死锁）。

## 关键设计取舍

- **天数轴不是历法**：`at.t` 是整数天数，`(vol, ch)` 只是它的粗粒度投影。
  R-TL 单调性**按 t 判**，旧数据（无 t）回退章序。
- **pending 与 plot_threads 不合并**：threads 的 `report_deadline` 是章节位置驱动，
  pending 的 `due` 是天数驱动；同一事实两边登记、经 `pending.thread` 互引。
- **到期只升提示强度，不硬插剧情**：与 ADR-002（串行逐章、大纲驱动）一致，
  系统不替作者决定"这一章必须写出关"。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .embedding import tokenize

TIMELINE_REL = "bible/timeline.json"

# ---------------------------------------------------------------- 天数解析

# 单事件时间增量上限（天）：超过即疑似抽取错误，warn 但不阻断（ADR-019）
MAX_DT_WARN = 3650

_UNIT_DAYS = {"日": 1, "天": 1, "周": 7, "月": 30, "个月": 30, "年": 365}
_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
           "八": 8, "九": 9, "十": 10}

# 模糊量词归一兜底（docs/06 §3.3.2）：prompt 要求 LLM 给具体数字，这里兜抽取噪声
_QUANTIFIER: dict[str, int] = {
    "一日": 1, "一天": 1, "两日": 2, "两天": 2, "数日": 2, "数天": 2, "几日": 2, "几天": 2,
    "一周": 7, "半月": 15, "一月": 30, "一个月": 30, "两月": 60, "两个月": 60,
    "三月": 90, "三个月": 90, "半年": 180, "一年": 365, "两年": 730, "三年": 1095,
    "五年": 1825, "十年": 3650,
}

# 闪回/插叙：不推进 now（回忆发生在过去，但当前时刻没动）
_FLASHBACK = frozenset({"闪回", "回忆", "插叙", "倒叙", "回溯", "当年", "那年"})
# 同日/多线并进：dt = 0
_SAME_DAY = frozenset({"同日", "当天", "当日", "同时", "与此同时", "当日稍后", "0", "+0"})


def parse_duration(raw: str) -> tuple[int | None, str | None]:
    """把时间增量文本解析成天数。返回 `(dt, warn)`。

    - `dt is None` 表示**不推进**（闪回/插叙，或完全无法解析）；
    - `warn` 非空表示解析出来了但可疑（超上限），交由调用方记入 report。
    """
    s = (raw or "").strip().strip("。. ")
    s = re.sub(r"后$", "", s).strip()
    if not s:
        return None, None
    if s in _FLASHBACK:
        return None, None
    if s in _SAME_DAY:
        return 0, None
    if s in _QUANTIFIER:
        return _QUANTIFIER[s], None
    m = re.fullmatch(r"\+?\s*(\d+)\s*(个?)(日|天|周|月|年)", s)
    if m:
        return int(m.group(1)) * _UNIT_DAYS[m.group(3)], None
    m = re.fullmatch(r"\+?\s*([一二三四五六七八九十两])\s*(个?)(日|天|周|月|年)", s)
    if m:
        return _CN_NUM[m.group(1)] * _UNIT_DAYS[m.group(3)], None
    m = re.fullmatch(r"\+?\s*(\d+)", s)
    if m:  # 裸数字按天计（prompt 要求带单位，这里兜底）
        return int(m.group(1)), None
    return None, f"时间增量无法解析：{raw!r}（已忽略，未推进时间）"


# ---------------------------------------------------------------- 抽取行解析

_TIME_LINE_RE = re.compile(r"^时间[:：]\s*(.+)$")
_PENDING_LINE_RE = re.compile(r"^约定[:：]\s*(.+)$")


def parse_time_line(text: str) -> tuple[int | None, str, str | None]:
    """解析编纂员「时间：」行。返回 `(dt, note, warn)`。

    支持：`时间：+90日`、`时间：+90日 | 叶蓝闭关结束`、`时间：闪回`、`时间：同日`。
    """
    m = _TIME_LINE_RE.match((text or "").strip())
    if not m:
        return None, "", None
    body = m.group(1).strip()
    note = ""
    if "|" in body or "｜" in body:
        head, note = re.split(r"[|｜]", body, maxsplit=1)
        body, note = head.strip(), note.strip()
    dt, warn = parse_duration(body)
    return dt, note, warn


def parse_pending_line(text: str) -> tuple[str, int, str | None] | None:
    """解析编纂员「约定：」行。返回 `(what, dt, warn)`，格式不符返回 None。

    支持：`约定：叶蓝出关｜+90日`（who 从 what 里按姓名表回扫，见 `resolve_who`）。
    """
    m = _PENDING_LINE_RE.match((text or "").strip())
    if not m:
        return None
    body = m.group(1).strip()
    if not ("|" in body or "｜" in body):
        return None
    what, tail = re.split(r"[|｜]", body, maxsplit=1)
    what, tail = what.strip(), tail.strip()
    if not what:
        return None
    dt, warn = parse_duration(tail)
    if dt is None or dt <= 0:
        return None
    return what, dt, warn


# ---------------------------------------------------------------- 读写

def now_of(state: dict) -> int:
    """当前时刻（天数）。缺 time 结构的旧数据回退 0。"""
    t = state.get("time")
    if isinstance(t, dict):
        try:
            return int(t.get("now") or 0)
        except (TypeError, ValueError):
            return 0
    return int(t) if isinstance(t, (int, float)) else 0


def pending_of(state: dict) -> list[dict]:
    """待办日程列表（只返回 dict 项，容忍脏数据）。"""
    ps = state.get("pending")
    if not isinstance(ps, list):
        return []
    return [p for p in ps if isinstance(p, dict)]


def _load(ws, project_id: str) -> dict:
    """读 worldstate（已由 `worldstate.load` 补齐 time/pending 结构）。"""
    from . import worldstate

    return worldstate.load(ws, project_id)


def _save(ws, project_id: str, state: dict) -> None:
    from . import worldstate

    worldstate.save(ws, project_id, state)


def load_timeline(ws, project_id: str) -> list[dict]:
    """读 `bible/timeline.json`（历史时点登记簿）。"""
    p = ws._abs(f"{project_id}/{TIMELINE_REL}")  # noqa: SLF001 - 同期存量接口，见 docs/11 §13 P0-3
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


def save_timeline(ws, project_id: str, entries: list[dict]) -> None:
    p = ws._abs(f"{project_id}/{TIMELINE_REL}")  # noqa: SLF001 - 同上
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


def append_timeline(ws, project_id: str, *, event: str, t: int, vol: int, ch: int,
                    dt: int = 0) -> dict:
    """登记一个历史时点。id 形如 `tl:12`（按已有最大序号 +1）。每次读盘——
    时间线是小文件，正确性优先于省一次 IO（批量写会让旧条目被覆盖）。

    `dt`：本时点由 advance() 推进的天数（0=登记不推进/旧数据无此字段）。
    留痕目的：`rollback_chapter` 按章回退时据此逆推 `now`（2026-09-05 幂等修复）。
    """
    data = load_timeline(ws, project_id)
    max_n = 0
    for e in data:
        m = re.match(r"^tl:(\d+)$", str(e.get("id") or ""))
        if m:
            max_n = max(max_n, int(m.group(1)))
    entry = {
        "id": f"tl:{max_n + 1}",
        "event": event,
        "at": {"t": int(t), "vol": int(vol), "ch": int(ch)},
        "in_chapters": [{"vol": int(vol), "ch": int(ch)}],
    }
    if dt:
        entry["dt"] = int(dt)
    data.append(entry)
    save_timeline(ws, project_id, data)
    return entry


# ---------------------------------------------------------------- 推进与登记

def advance(ws, project_id: str, dt: int, *, vol: int, ch: int, event: str = "") -> int:
    """推进故事时间 `dt` 天并登记 timeline 条目。返回推进后的 now。

    `dt <= 0`（同日/多线并进）只登记不推进——timeline 仍记录该时点，
    因为"同一天发生两件事"本身是事实。
    """
    state = _load(ws, project_id)
    now = now_of(state)
    new_now = now + int(dt) if dt > 0 else now
    state["time"]["now"] = new_now
    _save(ws, project_id, state)
    append_timeline(ws, project_id,
                    event=event or (f"时间推进 {dt} 日" if dt else "同日内推进"),
                    t=new_now, vol=vol, ch=ch, dt=int(dt) if dt > 0 else 0)
    return new_now


def rollback_chapter(ws, project_id: str, vol: int, ch: int) -> dict:
    """按章回退时间轴（章节重写前置清理，2026-09-05 幂等修复）。

    删除该章登记的全部 timeline 条目，并按条目 `dt` 逆推 `worldstate.time.now`
    （旧数据无 dt 字段则只删条目不回拨——宁保守不猜）。返回 `{"removed": n,
    "rewound": dt}`。
    """
    data = load_timeline(ws, project_id)
    kept, removed, rewound = [], 0, 0
    for e in data:
        at = e.get("at") if isinstance(e, dict) else None
        if (isinstance(at, dict) and int(at.get("vol", -1)) == vol
                and int(at.get("ch", -1)) == ch):
            removed += 1
            try:
                rewound += int(e.get("dt") or 0)
            except (TypeError, ValueError):
                pass
            continue
        kept.append(e)
    if removed:
        save_timeline(ws, project_id, kept)
        state = _load(ws, project_id)
        state["time"]["now"] = max(0, now_of(state) - rewound)
        _save(ws, project_id, state)
    return {"removed": removed, "rewound": rewound}


def resolve_who(what: str, name_to_id: dict[str, str]) -> str:
    """从约定文本里回扫人物 id（编纂员不保证给 id）。

    长名优先（"叶蓝"先于"蓝"），命中即返回；无命中返回空串（pending 允许无主 ——
    如"宗门大比开始"这类世界级日程）。
    """
    for nm in sorted((k for k in name_to_id if k), key=len, reverse=True):
        if nm in what:
            return name_to_id[nm]
    return ""


def add_pending(ws, project_id: str, *, what: str, dt: int, vol: int, ch: int,
                who: str = "", thread: str | None = None,
                unavailable_states: tuple[str, ...] = ()) -> dict:
    """登记一条未来日程（due = 当前 now + dt）。返回该条目。

    `unavailable_states` 非空且 `what` 命中（闭关/失踪/昏迷/被囚/渡劫…）→
    给 `who` 打 `unavailable_until = due`，R-STATE 据此校验"不可出场期不得出场"。
    """
    state = _load(ws, project_id)
    now = now_of(state)
    ps = state.setdefault("pending", [])
    max_n = 0
    for p in pending_of(state):
        m = re.match(r"^pd:(\d+)$", str(p.get("id") or ""))
        if m:
            max_n = max(max_n, int(m.group(1)))
    item: dict[str, Any] = {
        "id": f"pd:{max_n + 1}",
        "who": who or "",
        "what": what,
        "due": now + int(dt),
        "span": max(int(dt), 1),          # 跨度（用于 30%/10% 分档比例）
        "created_t": now,
        "status": "scheduled",            # scheduled|fired|cancelled|expired
        "created_at": {"vol": int(vol), "ch": int(ch)},
        "overdue": 0,                     # 到期后经过的章数
        "block_count": 0,                 # 软 block 次数（≥3 自动 expired）
    }
    if thread:
        item["thread"] = thread
    ps.append(item)
    if who and unavailable_states and any(k in what for k in unavailable_states):
        cur = (state.setdefault("characters", {}).setdefault(who, {}))
        if isinstance(cur, dict):
            cur["unavailable_until"] = item["due"]
            cur["unavailable_since"] = {"vol": int(vol), "ch": int(ch)}
            cur["unavailable_reason"] = next(k for k in unavailable_states if k in what)
    _save(ws, project_id, state)
    return item


# ---------------------------------------------------------------- 分档提醒

@dataclass
class Reminder:
    """一条到期提醒（供生成提示注入与告警使用）。"""

    item: dict
    level: str          # light | strong | overdue
    left: int           # due - now（天），负数表示已到期
    text: str


def _reminders(state: dict) -> list[Reminder]:
    now = now_of(state)
    out: list[Reminder] = []
    for p in pending_of(state):
        if p.get("status") != "scheduled":
            continue
        due = int(p.get("due") or 0)
        left = due - now
        span = max(int(p.get("span") or 1), 1)
        what = str(p.get("what") or "").strip()
        if left <= 0:
            out.append(Reminder(p, "overdue", left, f"{what}（已到期 {-left} 天）"))
        elif left / span <= 0.10:
            out.append(Reminder(p, "strong", left, f"{what}（还有 {left} 天）"))
        elif left / span <= 0.30:
            out.append(Reminder(p, "light", left, f"{what}（还有 {left} 天）"))
    return out


def reminder_lines(state: dict, *, max_lines: int = 6) -> list[str]:
    """按 docs/06 §3.3.3 三档生成提示行（确定性，零 LLM）。

    - > 30% 静默；≤ 30% 轻提示；≤ 10% 或已到期 强提示 + key_events 候选。
    - 到期只升提示强度、**不硬插剧情**（与 ADR-002 大纲驱动一致）。
    """
    rs = _reminders(state)
    if not rs:
        return []
    light = [r.text for r in rs if r.level == "light"]
    strong = [r.text for r in rs if r.level in ("strong", "overdue")]
    lines: list[str] = []
    if light:
        lines.append("【临近事项】" + "；".join(light[:max_lines]))
    if strong:
        lines.append("【本章宜安排】" + "；".join(strong[:max_lines])
                     + "——请在 key_events 中体现，否则将被时间规则拦截")
    return lines


# ---------------------------------------------------------------- 到期记账

@dataclass
class TickReport:
    """一章结束后的时间记账结果。"""

    fired: list[str] = field(default_factory=list)        # 正文已引出的 pending id
    warnings: list[str] = field(default_factory=list)     # 供 report/CLI 展示
    expired: list[str] = field(default_factory=list)      # 连续 block 后自动作废的 id
    blocked: list[str] = field(default_factory=list)      # 处于软 block 的 id
    now: int = 0

    @property
    def ok(self) -> bool:
        return not self.blocked

    def as_dict(self) -> dict:
        return {"fired": self.fired, "warnings": self.warnings,
                "expired": self.expired, "blocked": self.blocked, "now": self.now}


def match_pending(what: str, text: str) -> bool:
    """判断正文是否"引出"了某条 pending（token 重合度启发式）。

    不用 LLM：编纂员已经把正文抽成事件了，这里只做二元组重合判定。
    短文本（`what` 少于 5 个 token）命中 1 个即可，长文本要求 ≥2 且重合率 ≥30%，
    避免"叶蓝出关"被任意提到"出关"的章节误判兑现。
    """
    w = set(tokenize(what))
    t = set(tokenize(text or ""))
    hit = w & t
    if not hit:
        return False
    if len(w) >= 5 and len(hit) < 2:
        return False
    return len(hit) / len(w) >= 0.3


def blocked_pending(state: dict) -> list[dict]:
    """处于软 block 状态的 pending（overdue ≥ 5 章且未自动作废）。"""
    return [p for p in pending_of(state)
            if p.get("status") == "scheduled" and int(p.get("overdue") or 0) >= 5]


def soft_block_check(ws, project_id: str, planned_text: str) -> list[str]:
    """写章前的软 block 判定：被 block 的 pending 若未在本章计划（细纲/key_events）
    中出现，返回拦截理由列表；无拦截返回空列表。

    拦截**不是终止**——调用方拦截后须调一次 `tick()` 记账，连续 3 次自动
    `expired` 放行（防挂机批跑死锁，docs/06 §3.3.3）。
    """
    state = _load(ws, project_id)
    out: list[str] = []
    for p in blocked_pending(state):
        what = str(p.get("what") or "")
        if match_pending(what, planned_text):
            continue
        out.append(f"R-TIME 软 block：[{p.get('id')}]「{what}」已逾期 "
                   f"{int(p.get('overdue') or 0)} 章未兑现，本章细纲/key_events 须体现该事项")
    return out


def tick(ws, project_id: str, *, vol: int, ch: int, chapter_text: str = ""
         ) -> TickReport:
    """一章结束后的时间记账：兑现判定 + 到期章数递增 + 自动作废。

    - 正文 token 命中 pending.what → `fired`，并清除该人物的不可出场期；
    - **人物姓名在正文出场且已到期**（词面不重合兜底：约定写「闭关三月」、
      正文写「出关」——两个词没有共同二元组，须靠"姓名+已到期"兜底）→ `fired`；
    - 已到期未兑现 → `overdue += 1`；≥3 章 warn；≥5 章进软 block；
      软 block 累计 3 次 → 自动 `expired` 放行（留痕，事件不静默消失）。
    """
    state = _load(ws, project_id)
    rep = TickReport(now=now_of(state))
    names = _load_char_names(ws, project_id)
    changed = False
    for p in pending_of(state):
        if p.get("status") != "scheduled":
            continue
        what = str(p.get("what") or "")
        due = int(p.get("due") or 0)
        if _fired(p, what, due, chapter_text, rep.now, names):
            p["status"] = "fired"
            p["fired_at"] = {"vol": int(vol), "ch": int(ch)}
            rep.fired.append(str(p.get("id")))
            _clear_unavailable(state, p)
            changed = True
            continue
        if rep.now < due:
            continue
        # 幂等护栏（2026-09-05）：同一章重跑不得重复递增 overdue——
        # 否则重跑一次多吃一章宽限，把本还有余量的日程提前打成软 block。
        ch_key = f"{int(vol)}-{int(ch)}"
        counted = p.setdefault("overdue_by", [])
        if not isinstance(counted, list):
            counted = p["overdue_by"] = []
        if ch_key in counted:
            continue
        counted.append(ch_key)
        od = int(p.get("overdue") or 0) + 1
        p["overdue"] = od
        changed = True
        if od >= 5:
            bc = int(p.get("block_count") or 0) + 1
            p["block_count"] = bc
            if bc >= 3:
                p["status"] = "expired"
                p["expired_at"] = {"vol": int(vol), "ch": int(ch)}
                rep.expired.append(str(p.get("id")))
                rep.warnings.append(
                    f"[{p.get('id')}]「{what}」逾期 {od} 章未兑现，已自动作废（expired）——"
                    "建议人工处理或改期，系统不再拦截")
                _clear_unavailable(state, p)
            else:
                rep.blocked.append(str(p.get("id")))
                rep.warnings.append(
                    f"[{p.get('id')}]「{what}」逾期 {od} 章未兑现（软 block {bc}/3）——"
                    "本章须在 key_events 中体现，否则不予放行")
        elif od >= 3:
            rep.warnings.append(
                f"[{p.get('id')}]「{what}」已到期 {od} 章未兑现，该安排兑现了")
    if changed:
        _save(ws, project_id, state)
    return rep


def _load_char_names(ws, project_id: str) -> dict[str, str]:
    """读 bible/characters.json 建 id → 姓名 表（兑现兜底用）。"""
    import json

    p = ws._abs(f"{project_id}/bible/characters.json")  # noqa: SLF001
    if not p.exists():
        return {}
    try:
        chars = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    if not isinstance(chars, list):
        return {}
    return {str(c.get("id")): str(c.get("name") or "")
            for c in chars if isinstance(c, dict) and c.get("id")}


def _fired(item: dict, what: str, due: int, text: str, now: int,
           names: dict[str, str]) -> bool:
    """兑现判定：token 重合优先；词面不重合时「人物出场 + 已到期」兜底。

    兜底的合理性：强提示此前已把该事项注入 key_events 候选，作者把该人物
    写上台本身就是对节点的回应（「闭关三月 → 出关」两词无共同二元组）。
    代价是轻微误判（人物恰好出场但没处理节点）——pending 记 fired 只是
    停止提醒，损失可接受，且符合"到期只升提示强度、不硬插剧情"。
    """
    if match_pending(what, text):
        return True
    who = item.get("who")
    nm = names.get(str(who)) if who else ""
    return bool(nm and due <= now and nm in (text or ""))


def _clear_unavailable(state: dict, item: dict) -> None:
    """pending 兑现/作废后解除人物的不可出场期。"""
    who = item.get("who")
    cur = (state.get("characters") or {}).get(who) if who else None
    if isinstance(cur, dict):
        cur.pop("unavailable_until", None)
        cur.pop("unavailable_since", None)
        cur.pop("unavailable_reason", None)


def pending_summary_lines(state: dict) -> list[str]:
    """供 CLI/report 展示的日程清单行（不影响生成）。"""
    lines: list[str] = []
    for p in pending_of(state):
        lines.append(f"- [{p.get('id')}] {p.get('what')} · due=t+{p.get('due')} · "
                     f"{p.get('status')}"
                     + (f" · 逾期 {p.get('overdue')} 章" if int(p.get("overdue") or 0) else ""))
    return lines
