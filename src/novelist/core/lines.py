"""线索（Line）子系统 —— bible/lines.json 账本（ADR-025，设计定稿 2026-09-06）。

## 它补的是什么缺口（docs/线索子系统设计与规划-2026-09-06.md §1）

lingyu5 真机实证：宗门暗线在 5 章 7654 字中仅 3 次名词提及、零处暗线场景。三个机理断点：

1. threads 语义=伏笔（planted→pending_return→returned），只问"何时收"，不问"这章在不在场"；
2. 章纲 threads_involved 填了但 core/ 零消费，事件注入靠 RAG 命中（未命中=沉默）；
3. 人物有 director 调度层（ADR-020），线索没有。

## 设计要点（已拍板决策，全量见设计文档 §0）

- **一份账本四级视图**：卷纲（dormant 全集+前卷 yield）/ 章纲（active 全量，唯一决策层）/
  事件生成（只给本场命中线，钉死层 priority -1）/ 润色（禁令一行）。
- **单一文件**（ADR-016 文件=事实源），字段按 ADR-011 分应然（kind/carrier/scope/target）
  与实然（status/opened/last_seen/progress/yield/closed）两组。
- **起止三级落定**：蓝图骨架登记(dormant) → 卷纲开线计划 → 章纲 lines_present 过人审
  = 事实开启点。生成期模型只有提名权（Chronicler 记 pending → 人工转正）。
- **事件始、事件终**：闭合主路径=Chronicler 确认+确定性校验；计划外闭合=closing_candidate，
  章末人工确认；closed 留档不删，一致性检查拦"死线复活"。
- **一硬多警**：主线唯一=硬校验（ValueError）；活跃支线≤3 / 收尾期禁开线 / 冷却超阈=告警。
- **失败降级**（ADR-021 纪律）：任何异常降级为现状行为，绝不阻断生成。

微线（1-2 章自生自灭）不入账——判据三问（贯穿/串联/价值）写在 Chronicler 提名 prompt
与人工转正清单里，本模块只做账本机械操作。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

LEDGER_REL = "bible/lines.json"

# 实然状态：dormant(登记未开) → active ⇄ suspended → closed；pending=生成期提名待审
STATUSES = ("dormant", "pending", "active", "suspended", "closed")
# 章纲 lines_present 动作（人审后由 apply_chapter_actions 落账）
CHAPTER_ACTIONS = ("open", "advance", "suspend", "flicker", "close")
# Chronicler 线索行动作（正文回写；open 仅对 dormant/pending 生效，其余走提名）
CHRONICLER_ACTIONS = ("open", "advance", "close", "weave", "reverse")
KINDS = ("main", "subplot", "hidden")
CARRIERS = ("object", "goal", "character", "emotion", "faction", "theme")

KIND_CN = {"main": "主线", "subplot": "支线", "hidden": "暗线"}
ACTION_CN = {"open": "开", "advance": "推", "close": "闭", "weave": "交织",
             "reverse": "反转", "suspend": "挂起", "flicker": "露头"}

# 分档冷却（告警级）：main 2-3 章内必须 flicker/advance；subplot 5 章；hidden 跨卷合法
COOLDOWN_CH = {"main": 3, "subplot": 5, "hidden": 0}   # 0 = 不查（伏笔型静默合法）
MAX_ACTIVE_SUBPLOTS = 3        # 活跃支线预算（告警级）
MAX_EVENT_LINES = 3            # 单事件注入线卡上限
PROGRESS_TAIL = 2              # 单行卡携带的进度尾条数


# ---------------------------------------------------------------------------
# 账本读写（缺文件=空账本，安全降级）
# ---------------------------------------------------------------------------

def lines_path(ws, project_id: str) -> Path:
    return ws._abs(f"{project_id}/{LEDGER_REL}")  # noqa: SLF001


def load_lines(ws, project_id: str) -> list[dict]:
    """读账本；缺失/损坏返回 []（缺文件=空账本，一切调用方照常工作）。"""
    p = lines_path(ws, project_id)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    return [row for row in data if isinstance(row, dict) and row.get("id")] \
        if isinstance(data, list) else []


def save_lines(ws, project_id: str, lines: list[dict]) -> None:
    """原子性由 ws.write_json 承担（临时文件+替换）；调用方持整本改后整体写回。"""
    ws.write_json(lines_path(ws, project_id), list(lines))


def new_line(ln_id: str, desc: str, *, kind: str = "subplot", carrier: str = "",
             scope: str = "book", members: list[str] | None = None,
             target: dict | None = None, status: str = "dormant") -> dict:
    """骨架登记工厂（蓝图 book 节点 / 提名转正共用）。"""
    return {
        "id": str(ln_id),
        "desc": str(desc),
        "kind": kind if kind in KINDS else "subplot",
        "carrier": carrier if carrier in CARRIERS else "",
        "scope": scope if scope in ("book", "volume") else "book",
        "members": [str(m) for m in (members or []) if m],
        "status": status if status in STATUSES else "dormant",
        "opened": None,
        "last_seen": None,
        "progress": [],
        "target": dict(target) if isinstance(target, dict) else None,
        "yield": None,
        "closed": None,
        "closing_candidate": None,
    }


# ---------------------------------------------------------------------------
# 确定性校验（一硬多警中的"硬"与一致性拦截）
# ---------------------------------------------------------------------------

def validate_lines(lines: list[dict]) -> list[str]:
    """返回错误列表（空 = 账本合法）。主线唯一与 main.target 必填是硬约束。"""
    errs: list[str] = []
    seen: set[str] = set()
    mains: list[str] = []
    for ln in lines:
        lid = str(ln.get("id") or "")
        if not lid:
            errs.append("存在无 id 的线索行")
            continue
        if lid in seen:
            errs.append(f"线索 id 重复：{lid}")
        seen.add(lid)
        if ln.get("kind") not in KINDS:
            errs.append(f"{lid}: kind 非法（{ln.get('kind')!r}）")
        if ln.get("status") not in STATUSES:
            errs.append(f"{lid}: status 非法（{ln.get('status')!r}）")
        if ln.get("kind") == "main":
            mains.append(lid)
            tg = ln.get("target")
            if not (isinstance(tg, dict) and tg.get("vol")):
                errs.append(f"{lid}: main 缺 target（{{vol, note}} 必填）")
    if len(mains) > 1:
        errs.append(f"主线不唯一（{len(mains)} 条）：{ '、'.join(mains) }——主线唯一是硬约束")
    # closed 一致性：closed 置位 ⇔ status=closed（死线复活在此拦）
    for ln in lines:
        lid = str(ln.get("id") or "")
        if ln.get("status") == "closed" and not ln.get("closed"):
            errs.append(f"{lid}: status=closed 但缺 closed 落点")
        if ln.get("closed") and ln.get("status") != "closed":
            errs.append(f"{lid}: 已有 closed 落点但 status={ln.get('status')!r}（死线复活）")
    return errs


def is_dead(line: dict) -> bool:
    return line.get("status") == "closed" or bool(line.get("closed"))


def active_lines(lines: list[dict]) -> list[dict]:
    return [ln for ln in lines if ln.get("status") == "active"]


# ---------------------------------------------------------------------------
# 冷却与视图（四级注入）
# ---------------------------------------------------------------------------

def ch_gap(cur: tuple[int, int], last: dict | None, k: int) -> int | None:
    """(vol,ch) 距离，按每卷 K 章折算全局章距；无法折算返回 None。"""
    if not (isinstance(last, dict) and last.get("vol") and k > 0):
        return None
    try:
        return (int(cur[0]) - int(last["vol"])) * k + (int(cur[1]) - int(last["ch"]))
    except (TypeError, ValueError):
        return None


def cooldown_warnings(lines: list[dict], vol: int, ch: int, k: int) -> list[str]:
    """active 线超分档冷却 → 告警行（强制"露面或显式挂起"二选一）。"""
    out: list[str] = []
    for ln in active_lines(lines):
        limit = COOLDOWN_CH.get(str(ln.get("kind")), 0)
        if not limit:
            continue
        gap = ch_gap((vol, ch), ln.get("last_seen") or ln.get("opened"), k)
        if gap is not None and gap > limit:
            out.append(f"{ln['id']}（{KIND_CN.get(ln.get('kind'), '?')}·上次"
                       f"v{ln.get('last_seen') or ln.get('opened')}，距 {gap} 章 > {limit} 章冷却）")
    return out


def line_card(ln: dict, vol: int, ch: int, k: int, *, note: str = "") -> str:
    """单行紧凑卡（不给 desc 全文、不给全量 progress——模型只要当前状态+本章目标）。"""
    kind = KIND_CN.get(str(ln.get("kind")), "线")
    last = ln.get("last_seen") or ln.get("opened")
    seen = f"上次{last['vol']}-{last['ch']}" if isinstance(last, dict) and last.get("vol") else "未出场"
    gap = ch_gap((vol, ch), last, k)
    if gap is not None:
        seen += f"(距{gap}章)"
    prog = [str(p) for p in (ln.get("progress") or []) if str(p).strip()][-PROGRESS_TAIL:]
    prog_txt = "→".join(prog) if prog else "（无进度）"
    card = f"{ln['id']} {kind}·{ln.get('status')} | {seen} | {prog_txt}"
    if note:
        card += f" | 本章:{note}"
    return card


def chapter_view(lines: list[dict], vol: int, ch: int, k: int, *,
                 tail_phase: bool = False, opening_phase: bool = False,
                 ) -> tuple[str, list[str]]:
    """章纲层视图（唯一决策层）：active 全量单行卡 + 冷却告警置顶 + closed 禁复活负清单。

    返回 (注入块, 告警列表)。无账本/无 active 线时返回 ("", [])——不注入，行为与旧版一致。
    """
    warns: list[str] = []
    ledger = [ln for ln in lines if not is_dead(ln) and ln.get("status") != "pending"]
    active = active_lines(ledger)
    if not active:
        return "", warns
    cd = cooldown_warnings(lines, vol, ch, k)
    warns.extend(f"冷却告警：{x}——本章必须露面（advance/flicker）或显式挂起（suspend）" for x in cd)
    if tail_phase and any(ln.get("status") == "dormant" for ln in ledger):
        warns.append("收尾期告警：本章不得开新线（open 动作将被拒绝）")
    if opening_phase:
        hid = [ln["id"] for ln in ledger if ln.get("kind") == "hidden"
               and ln.get("status") == "dormant"]
        if hid:
            warns.append("开篇期告警：暗线只许埋（提及）不许揭开真相——" + "、".join(hid))

    cd_set = {w.split("（")[0] for w in cd}
    ordered = sorted(
        active,
        key=lambda x: (0 if x["id"] in cd_set else 1,
                       0 if x.get("kind") == "main" else 1,
                       (x.get("last_seen") or {}).get("vol", 99),
                       (x.get("last_seen") or {}).get("ch", 99)))
    rows = [line_card(ln, vol, ch, k) for ln in ordered]
    block = "【进行中的线索】（每条都要按本章动作处理：推进剧情，不是复述进度）\n" + "\n".join(rows)
    closed = [ln for ln in lines if is_dead(ln)]
    if closed:
        block += ("\n【已闭合线索·禁复活】下列线索已收束，不得再当活线写、不得让它"
                  "产生新剧情：" + "、".join(str(ln.get("id")) for ln in closed))
    return block, warns


def event_view(lines: list[dict], declared_ids: list[str], ev_text: str,
               vol: int, ch: int, k: int) -> list[str]:
    """事件层视图（唯一执行层）：只给本场命中的线卡（钉死层）。

    命中判定 = 章纲 lines_present 声明优先，其余按 desc/members 词元命中事件文本；
    closed 线永不注入；≤MAX_EVENT_LINES 条，声明优先、其余按 last_seen 升序。
    ≥2 条时标注交织（"本事件=X线×Y线交织点"）。
    """
    if not lines:
        return []
    by_id = {str(ln.get("id")): ln for ln in lines}
    hits: list[dict] = []
    for lid in declared_ids or []:
        ln = by_id.get(str(lid))
        if ln is not None and not is_dead(ln) and ln not in hits:
            hits.append(ln)
    if len(hits) < MAX_EVENT_LINES and ev_text:
        from .embedding import tokenize

        ev_toks = set(tokenize(ev_text))
        if ev_toks:
            rest = [ln for ln in lines
                    if not is_dead(ln) and ln.get("status") == "active"
                    and ln not in hits]
            rest.sort(key=lambda x: ((x.get("last_seen") or {}).get("vol", 99),
                                     (x.get("last_seen") or {}).get("ch", 99)))
            for ln in rest:
                hay = " ".join([str(ln.get("desc") or ""), *map(str, ln.get("members") or [])])
                if ev_toks & set(tokenize(hay)):
                    hits.append(ln)
                    if len(hits) >= MAX_EVENT_LINES:
                        break
    cards = [line_card(ln, vol, ch, k) for ln in hits]
    if len(hits) >= 2:
        ids = "×".join(str(ln.get("id")) for ln in hits[:2])
        cards.append(f"※ 本事件 = {ids} 交织点（两条线在同一事件里互相作用，不是各写各的）")
    return cards


# ---------------------------------------------------------------------------
# 账本变更（章纲人审动作 / Chronicler 回写 / 提名转正）
# ---------------------------------------------------------------------------

def _prog_entry(vol: int, ch: int, note: str, *, mark: str = "") -> str:
    tag = f"[{mark}]" if mark else ""
    return f"{vol}-{ch} {tag}{note}".strip()


def _append_progress(ln: dict, vol: int, ch: int, note: str, *, mark: str = "") -> None:
    entry = _prog_entry(vol, ch, note, mark=mark)
    prog = ln.setdefault("progress", [])
    if prog and prog[-1] == entry:
        return  # 同章同文只记一次（逐事件编纂会重复抽取同一闭合/露头）
    prog.append(entry)


def apply_chapter_actions(ws, project_id: str, lines: list[dict], vol: int, ch: int,
                          actions: list[dict], *, tail_phase: bool = False,
                          opening_phase: bool = False) -> list[str]:
    """章纲 lines_present（人审后）落账。返回告警列表。

    actions 形如 [{"id": "ln:xxx", "action": "open|advance|suspend|flicker|close",
    "note": "一句话目标/动作"}]。悬空 id / closed 线操作 / 收尾期 open → 告警并跳过
    （告警级，绝不 raise——细纲落盘不受阻，账本不写脏）。
    """
    warns: list[str] = []
    by_id = {str(ln.get("id")): ln for ln in lines}
    changed = False
    for act in actions or []:
        if not isinstance(act, dict):
            continue
        lid, action = str(act.get("id") or ""), str(act.get("action") or "")
        note = str(act.get("note") or "").strip()
        if action not in CHAPTER_ACTIONS:
            warns.append(f"{lid}: 未知线索动作 {action!r}，忽略")
            continue
        ln = by_id.get(lid)
        if ln is None:
            warns.append(f"{lid}: 账本无此线（lines_present 声明悬空），忽略")
            continue
        if is_dead(ln):
            warns.append(f"{lid}: 死线复活拦截（已 closed，{action} 被拒）")
            continue
        if action == "open":
            if tail_phase:
                warns.append(f"{lid}: 收尾期禁开线（告警），open 已拒绝")
                continue
            if ln.get("kind") == "hidden" and opening_phase:
                warns.append(f"{lid}: 开篇期暗线只埋不揭，open 已按露头(flicker)处理")
                action = "flicker"
            if ln.get("status") == "active":
                action = "advance"   # 已在场 → 视同推进
            else:
                ln["status"] = "active"
                ln["opened"] = {"vol": vol, "ch": ch}
        if action == "advance" and ln.get("status") == "suspended":
            ln["status"] = "active"  # 显式恢复
        if action == "suspend":
            if ln.get("status") == "active":
                ln["status"] = "suspended"
                if note:
                    ln["resume_hint"] = note[:60]
        if action == "close":
            ln["status"] = "closed"
            ln["closed"] = {"vol": vol, "ch": ch, "reason": note[:80] or "章纲计划闭合"}
        if action in ("advance", "flicker", "open"):
            ln["last_seen"] = {"vol": vol, "ch": ch}
            _append_progress(ln, vol, ch, note or ACTION_CN[action], mark=ACTION_CN[action])
        changed = True
    if changed:
        try:
            save_lines(ws, project_id, lines)
        except Exception:  # noqa: BLE001 - 落盘失败降级，不阻断细纲
            warns.append("lines.json 落盘失败（账本未更新）")
    return warns


def register_pending(lines: list[dict], ln_id: str, desc: str, *,
                     kind: str = "subplot", members: list[str] | None = None) -> bool:
    """生成期提名：新线只进 pending 区，不进 active（模型没有开线权）。已存在→False。"""
    lid = str(ln_id or "").strip()
    if not lid or any(str(ln.get("id")) == lid for ln in lines):
        return False
    lines.append(new_line(lid, desc, kind=kind, members=members, status="pending"))
    return True


def confirm_pending(lines: list[dict], ln_id: str, *, vol: int, ch: int) -> bool:
    """人工转正：pending → dormant（后续由章纲 open 落定开启点）。"""
    ln = next((x for x in lines if str(x.get("id")) == str(ln_id)), None)
    if ln is None or ln.get("status") != "pending":
        return False
    ln["status"] = "dormant"
    ln.setdefault("progress", []).append(f"{vol}-{ch} 提名转正（人工确认入账）")
    return True


def apply_extracted_rows(ws, project_id: str, lines: list[dict], rows: list[dict],
                         vol: int, ch: int) -> dict:
    """Chronicler 线索行回写（事件始、事件终）。返回变更摘要供 report 呈现。

    rows 形如 [{"id": "ln:xxx", "action": "open|advance|close|weave|reverse",
    "note": "一句话"}]。确定性校验：
    - 账本外 id → 提名 pending（模型没有开线权，下一章细纲审批时人工转正）；
    - 闭合校验：与 target 计划吻合（target.vol == 当前卷）→ 自动闭合留痕；
      计划外 → closing_candidate（status 不变），章末汇总人工确认；
    - closed 线再被推动 → 死线复活告警，丢弃。
    """
    out: dict[str, list[str]] = {"changed": [], "candidates": [], "pending": [],
                                 "warnings": []}
    by_id = {str(ln.get("id")): ln for ln in lines}
    dirty = False
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        lid = str(row.get("id") or "").strip()
        action = str(row.get("action") or "").strip()
        note = str(row.get("note") or "").strip()[:80]
        if not lid or action not in CHRONICLER_ACTIONS:
            continue
        ln = by_id.get(lid)
        if ln is None:
            if register_pending(lines, lid, note or "（正文生成期提名，无描述）"):
                by_id[lid] = lines[-1]
                out["pending"].append(f"{lid}：{note}" if note else lid)
                dirty = True
            continue
        if is_dead(ln):
            out["warnings"].append(f"死线复活拦截：{lid} 已 closed，正文回写 {action} 被拒")
            continue
        if action == "open":
            if ln.get("status") in ("dormant", "pending"):
                ln["status"] = "active"
                ln["opened"] = {"vol": vol, "ch": ch}
                out["changed"].append(f"{lid} 开线：{note}")
            elif ln.get("status") == "active":
                action = "advance"   # 已在场 → 视同推进
            else:
                continue
        if action == "advance":
            if ln.get("status") not in ("active", "suspended"):
                continue
            ln["last_seen"] = {"vol": vol, "ch": ch}
            _append_progress(ln, vol, ch, note or "推进", mark="推")
            out["changed"].append(f"{lid} 推进：{note}" if note else f"{lid} 推进")
        elif action in ("weave", "reverse"):
            if ln.get("status") != "active":
                continue
            ln["last_seen"] = {"vol": vol, "ch": ch}
            _append_progress(ln, vol, ch, note or ACTION_CN[action], mark=ACTION_CN[action])
            out["changed"].append(f"{lid} {ACTION_CN[action]}：{note}")
        elif action == "close":
            target = ln.get("target") or {}
            planned = False
            try:
                planned = bool(target) and int(target.get("vol") or 0) == int(vol)
            except (TypeError, ValueError):
                planned = False
            if planned:
                ln["status"] = "closed"
                ln["closed"] = {"vol": vol, "ch": ch, "reason": note or "事件闭合（计划内）"}
                out["changed"].append(f"{lid} 闭合（计划内）：{note}")
            else:
                ln["closing_candidate"] = {"vol": vol, "ch": ch, "note": note}
                out["candidates"].append(f"{lid} 计划外闭合候选：{note}"
                                         f"（target={target or '未设'}，待人工确认）")
        dirty = True
    if dirty:
        try:
            save_lines(ws, project_id, lines)
        except Exception:  # noqa: BLE001 - 落盘失败降级
            out["warnings"].append("lines.json 落盘失败（账本未更新）")
    return out


# ---------------------------------------------------------------------------
# 解析：Chronicler 线索行 / 章纲 lines_present（细纲 md 行内 JSON）
# ---------------------------------------------------------------------------

_LINE_ROW_RE = re.compile(r"^\s*线索[:：]\s*(.+)$")


def parse_line_rows(content: str) -> list[dict]:
    """从编纂员输出解析「线索：ln:xxx | 推 | 一句话」行 → dicts。

    容错：动作取首段中的中文/英文动词映射，识别不了的行丢弃（宁少勿错）。
    """
    alias = {"开": "open", "推": "advance", "闭": "close", "交织": "weave",
             "反转": "reverse", "挂起": "suspend", "露头": "flicker"}
    rows: list[dict] = []
    for ln in (content or "").splitlines():
        m = _LINE_ROW_RE.match(ln.strip())
        if not m:
            continue
        parts = [p.strip() for p in m.group(1).split("|")]
        if len(parts) < 2:
            continue
        lid = parts[0]
        if not lid.startswith("ln:"):
            lid = f"ln:{lid}"
        action = alias.get(parts[1]) or (parts[1] if parts[1] in CHRONICLER_ACTIONS else "")
        if not action:
            continue
        rows.append({"id": lid, "action": action,
                     "note": parts[2].strip() if len(parts) > 2 else ""})
    return rows


def parse_line_decl(gist_text: str) -> list[dict]:
    """从细纲 md 解析行内「本章线索: [ {...} ]」（render_gist_md 写入的 JSON 数组）。"""
    m = re.search(r"^本章线索[:：]\s*(\[.*\])\s*$", gist_text or "", re.M)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return []
    out: list[dict] = []
    for x in data if isinstance(data, list) else []:
        if isinstance(x, dict) and str(x.get("id") or "").startswith("ln:"):
            out.append({"id": str(x["id"]),
                        "action": str(x.get("action") or "advance"),
                        "note": str(x.get("note") or "")})
    return out
