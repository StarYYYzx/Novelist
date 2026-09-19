"""常驻会话壳（P1：探讨设定 + /build 触发构建；docs/10 后续 §5.4）。

与 `run_consult` 的差异：run_consult 是"缺口收敛即退出"的批式循环；
本会话壳是**常驻 REPL**——用户自由输入（自由语 → 分派写槽 / 补充设想
登记 extras）或命令（/exit /show /help /build /save），直到显式 /exit 才退出，
缺口是否收敛只作提示。后续阶段（读章 / 修订建议链 / 大纲细纲审核）在此壳上
逐步接线。

设计边界（ADR-032 延续）：问什么由 `detect_gaps` 确定性决定；自由语由单次
LLM 分派听懂；写回/校验始终走 ask 的确定性护栏（`_dispatch_value` 等）。
命令分发是引擎外的键盘映射，不授予 LLM 任何自主控制权。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..storage.workspace import Workspace
from .ask import (  # noqa: PLC2701 - 复用 ask 的已有护栏/分派，避免复制
    _apply_slot_value, _build_questions, _default_of, _dispatch_value,
    _extras_banner, _fill_one_recommended, _llm_dispatch, _record_extras,
    _render_bp_overview, _render_round, _RETRY_LIMIT, _settle_round,
    split_multi_enum)
from .io_console import AnswerIO, ConsoleIO
from .review import (
    approve_all as review_approve_all,
    load_review, resolve_all_pending, resolve_pending, stage_pending_for_revise,
)
from .slots import Slot, default_slots, detect_gaps
from .state import Blueprint, ForgeState, append_transcript

_MAX_WINDOW = 3

HELP_TEXT = """会话命令（一律以 / 开头）：
  /exit           退出会话（未收敛缺口保留，退到 consulting 待 resume）
  /show           查看已填设定概览 + 补充设想登记状态
  /review [模块]   查看待审模块（无参列 pending，给模块名看全文）
  /approve 模块|all [--remember]  审核通过（all=批量批准全部待审；--remember=永久关开关）
  /approve-all 模块|all [off]  放行机制：批准待审并永久关闭该模块审核；加 off 恢复审核
  /revise 模块"建议"  按建议重生成并经审批（仍待审）
  /build          触发构建（仍缺 required 时先补齐推荐值并确认再构建）
  /conflicts      列出待裁决结构冲突（第二条主线 / 伏笔近重复）
  /resolve <id> <选项>  裁决一条冲突（选项与建议见 /conflicts）
  /save           手动保存蓝图
  /help           本帮助
自由语（非 / 开头的输入）：回答本轮缺口 / 补充设想（落不到槽位会登记 extras）。
空回车 = 本轮取推荐值推进。"""


@dataclass
class ShellResult:
    ok: bool
    answered: int = 0  # 本次会话采纳槽位数
    free_answers: int = 0
    extras_seen: int = 0
    rounds_done: int = 0
    build_triggered: bool = False
    quit_early: bool = False  # 有未收敛缺口时 /exit
    warnings: list[str] = field(default_factory=list)


def _banner(state: ForgeState, gaps: list[Any]) -> str:
    req = sum(1 for g in gaps if g.slot.level == "required")
    return (
        f"[novelist shell] 阶段={state.stage} 缺口={len(gaps)}（required={req}）\n"
        f"  /help 看命令；自由语回答/补充；缺口收敛与否由你 /build 决定。"
    )


def run_shell(ws: Workspace, project_id: str, bp: Blueprint, *,
              provider, io: AnswerIO | None = None,
              slots: list[Slot] | None = None,
              max_calls: int | None = None) -> ShellResult:
    """常驻会话主循环（P1）。自由语分派 + 确定性护栏 + /build 触发构建。"""
    io = io or ConsoleIO()
    if not io.is_tty:
        raise RuntimeError("shell 需要交互终端（TTY）；非交互请用 `forge resume` 的降级模式")
    slots = slots or default_slots()
    state = ForgeState.load(ws, project_id)
    result = ShellResult(ok=True)
    attempts: dict[str, int] = {}
    answered_keys: set[str] = set()
    round_no = 0

    io.notify(_banner(state, detect_gaps(bp, slots)))
    # 同一提示不重复刷屏（2026-09-16）：无缺口态此前**每敲一条命令就打一遍**，
    # 实测一次会话打 20+ 次，把真正的输出挤没了。
    last_notice: str | None = None
    while True:
        gaps = detect_gaps(bp, slots)
        window = [g.slot for g in gaps
                  if g.slot.key not in answered_keys][:_MAX_WINDOW]

        if window:
            round_no += 1
            last_notice = None
            qs = _build_questions(bp, window, provider, round_no, ws, project_id,
                                  result.warnings, need_candidates=True)
            io.notify(_render_round(round_no, qs))
            io.notify("CR> 回车=取推荐值 / 自由语=回答或补充设想 / /help")
        else:
            qs = []
            if last_notice != "no-gap":
                io.notify("\n（本蓝图已无缺口：/build 构建，或自由语继续补充设想登记 extras）")
                last_notice = "no-gap"

        line = io.ask_free("", "")
        if line is None:
            line = "/exit"

        raw = line.strip()
        # ---- 命令分发（确定性，无 LLM 控制权）----
        # 会话命令一律以 / 开头；其余输入/空回车一律视为自由语（回答或补充设想）。
        if raw.startswith("/"):
            # 统一斜杠前缀：/exit /show /build /save /review /approve /revise /help
            key, _, arg = raw[1:].partition(" ")
            key = key.lower().strip()
            arg = arg.strip()
            if key in ("exit", "quit"):
                if gaps:
                    state.stage = "consulting"
                    state.save(ws, project_id)
                    result.quit_early = True
                    result.warnings.append(
                        "会话结束：仍有未收敛缺口（stage=consulting）。"
                        "继续用 `novelist shell` 或 `forge resume`，或直接 `forge build`。"
                    )
                else:
                    state.touch_stage(ws, project_id, "seeded")
                return result
            if key in ("help", "h"):
                io.notify(HELP_TEXT)
                continue
            if key in ("show",):
                io.notify(_render_bp_overview(bp, slots) + _extras_banner(ws, project_id))
                continue
            if key in ("build",):
                _maybe_build(ws, project_id, bp, provider, io, state, gaps, slots,
                             max_calls, result)
                continue
            if key in ("conflicts",):
                _cmd_conflicts(ws, project_id, io)
                continue
            if key in ("resolve",):
                _cmd_resolve(ws, project_id, io, arg)
                continue
            if key in ("save",):
                bp.save(ws, project_id)
                io.notify("[shell] 蓝图已保存。")
                continue
            if key in ("review",):
                _cmd_review(ws, project_id, io, arg)
                continue
            if key in ("approve",):
                _cmd_approve(ws, project_id, io, arg)
                continue
            if key in ("approve-all", "approve_all"):
                _cmd_approve_all(ws, project_id, io, arg)
                continue
            if key in ("revise",):
                if not provider:
                    io.notify("[shell] revise 需要 LLM（provider 未配置）")
                    continue
                _cmd_revise(ws, project_id, provider, io, arg)
                continue
            io.notify(f"[shell] 未知命令 /{key}——/help 查看。")
            continue

        # ---- 自由语（含空回车=取推荐值）----
        if not window:
            # 无缺口（或全部已答）：无法分派到槽，直接登记为补充设想
            if raw:
                added = _record_extras(ws, project_id, [raw])
                result.extras_seen += added
                io.notify(f"[shell] 已登记 {added} 条补充设想（extras 待确认）。")
            continue
        if not raw:
            _settle_round(bp, qs, provider, io, ws, project_id, result,
                          answered_keys, attempts, round_no, "ask.default")
            bp.save(ws, project_id)
            result.rounds_done += 1
            continue
        ok = _dispatch_free_text(bp, qs, raw, provider, ws, project_id,
                                 attempts, result, round_no, io, answered_keys)
        if not ok:
            _settle_round(bp, qs, provider, io, ws, project_id, result,
                          answered_keys, attempts, round_no, "dispatch.fallback")
        bp.save(ws, project_id)
        result.rounds_done += 1


def _cmd_conflicts(ws: Workspace, project_id: str, io: AnswerIO) -> None:
    """/conflicts：列出待裁决的结构冲突（2026-09-16 拍板：由用户裁决）。"""
    from .conflicts import open_conflicts

    items = open_conflicts(ws, project_id)
    if not items:
        io.notify("[shell] 无待裁决冲突。")
        return
    io.notify(f"[shell] 待裁决冲突 {len(items)} 条：")
    for c in items:
        io.notify(f"  {c['id']} [{c['kind']}] {c['summary']}")
        io.notify(f"      可选：{' | '.join(c['options'])}（建议 {c['suggested']}）")
    io.notify("  裁决：/resolve <id> <选项>")


def _cmd_resolve(ws: Workspace, project_id: str, io: AnswerIO, arg: str) -> None:
    """/resolve <id> <选项>：裁决一条冲突并写回蓝图 + bible。"""
    from .conflicts import open_conflicts, resolve_conflict

    parts = arg.split()
    if len(parts) != 2:
        io.notify("[shell] 用法：/resolve <冲突id> <选项>（选项见 /conflicts）")
        return
    try:
        note = resolve_conflict(ws, project_id, parts[0], parts[1])
    except ValueError as e:
        io.notify(f"[shell] 裁决失败：{e}")
        return
    io.notify(f"[shell] {parts[0]} → {parts[1]}：{note}")
    io.notify(f"  剩余待决 {len(open_conflicts(ws, project_id))} 条")


def _notify_pending_left(ws: Workspace, project_id: str, io: AnswerIO) -> None:
    """回显剩余待审 + 下一步命令（2026-09-16 UX）。

    此前 `/approve X` 只回一句"已通过"，用户不知道还剩谁没过，只能反复 /review 试探。
    """
    pending = sorted((load_review(ws, project_id).get("pending") or {}))
    if pending:
        io.notify(f"  剩余待审：{'、'.join(pending)}——下一步 /review <模块> 看全文，"
                  "或 /approve <模块> 直接通过；全部放行用 /approve-all all")
    else:
        io.notify("  已无待审模块——可以 /build 继续构建。")


def _cmd_review(ws: Workspace, project_id: str, io: AnswerIO, arg: str) -> None:
    """/review [模块]：无参列 pending；给模块名显示该模块评审稿全文路径。"""
    cfg = load_review(ws, project_id)
    pending = cfg.get("pending") or {}
    if not arg:
        if not pending:
            io.notify("[shell] 无待审模块（pending 为空）。")
            return
        io.notify("[shell] 待审模块：")
        for name in sorted(pending):
            entry = pending[name]
            io.notify(f"  - {name}（{entry.get('vol','')}卷/{entry.get('ch','')}章，"
                      f"{entry.get('file','')}）")
        io.notify("  /review <模块名> 看全文；/approve <模块> 通过；/revise <模块> \"建议\"。")
        return
    if arg not in pending:
        # 2026-09-16 UX：此前只说"无此模块"，用户还得再敲一次 /review 才知道有哪些
        left = "、".join(sorted(pending)) if pending else "（无）"
        io.notify(f"[shell] 无此待审模块 {arg!r}；当前待审：{left}")
        return
    entry = pending[arg]
    md = ws._abs(f"{project_id}/{entry['file']}")
    try:
        text = md.read_text(encoding="utf-8")
    except OSError as e:  # noqa: BLE001
        io.notify(f"[shell] 读取评审稿失败：{e}")
        return
    io.notify(f"=== /review {arg}（{md}）===")
    io.notify(text)


def _cmd_approve(ws: Workspace, project_id: str, io: AnswerIO, arg: str) -> None:
    """/approve 模块|all [--remember]：通过审核模块（all=批量批准全部待审）。"""
    if not arg:
        io.notify('[shell] 用法：/approve <模块|all> [--remember]')
        return
    parts = arg.split()
    module = parts[0]
    remember = "--remember" in parts[1:]
    if module == "all":
        mods = resolve_all_pending(ws, project_id, remember=remember,
                                   note="shell approve all")
        if not mods:
            io.notify("[shell] 无待审模块。")
            return
        io.notify(f"[shell] 已批量通过 {len(mods)} 个模块：{'、'.join(mods)}"
                  + ("（永久关闭这些模块把关）" if remember else "") + "。")
        _notify_pending_left(ws, project_id, io)
        return
    cfg = load_review(ws, project_id)
    if module not in (cfg.get("pending") or {}):
        io.notify(f"[shell] 无此待审模块 {module!r}（/review 查看 pending）。")
        return
    try:
        resolve_pending(ws, project_id, module, decision="approve",
                        remember=remember, note="shell approve")
    except ValueError as e:  # noqa: BLE001
        io.notify(f"[shell] {e}")
        return
    io.notify(f"[shell] 已通过审核：{module}"
              + ("（永久关闭该模块把关）" if remember else "") + "。")
    _notify_pending_left(ws, project_id, io)


def _cmd_approve_all(ws: Workspace, project_id: str, io: AnswerIO, arg: str) -> None:
    """/approve-all 模块|all [off]：批准待审 + 永久关审核开关；off=恢复审核。"""
    if not arg:
        io.notify('[shell] 用法：/approve-all <模块|all> [off]')
        return
    parts = arg.split()
    target = parts[0]
    off = "off" in parts[1:]
    try:
        msg = review_approve_all(ws, project_id, target, off=off, note="shell approve-all")
    except ValueError as e:
        io.notify(f"[shell] {e}")
        return
    io.notify(f"[shell] {msg}")
    _notify_pending_left(ws, project_id, io)


def _cmd_revise(ws: Workspace, project_id: str, provider, io: AnswerIO,
                arg: str) -> None:
    """/revise 模块 "建议"：按建议重生成模块并展示差异（仍待审）。"""
    first = arg.split(None, 1)
    if len(first) < 2:
        io.notify('[shell] 用法：/revise <模块> "<修改建议>"')
        return
    module = first[0]
    suggestions = first[1].strip()
    from .engine import revise_module  # 延迟导入，避免顶层循环

    cfg = load_review(ws, project_id)
    try:
        diffs = revise_module(ws, project_id, module, suggestions, provider,
                              log_fn=lambda t: io.notify(f"  {t}"))
    except Exception as e:  # noqa: BLE001
        io.notify(f"[shell] revise 失败：{e}")
        return
    if not diffs:
        io.notify(f"[shell] {module} 无改动内容。")
        return
    io.notify(f"[shell] {module} 已按建议重生成（消耗 1 次 LLM 调用），"
              "差异如下（仍待审，/approve 通过）：")
    for d in diffs:
        io.notify(f"  {d}")
    if cfg.get("pending", {}).get(module):
        stage_pending_for_revise(ws, project_id, module, suggestions)
    _notify_pending_left(ws, project_id, io)


def _dispatch_free_text(bp: Blueprint, qs: list[Any], line: str, provider,
                        ws: Workspace, project_id: str, attempts: dict[str, int],
                        result: ShellResult, round_no: int, io: AnswerIO,
                        answered_keys: set[str]) -> bool:
    """自由语 → 单次 LLM 分派 → 确定性护栏写槽；失败返回 False（调用方取推荐值）。"""
    dispatch = _llm_dispatch(bp, qs, line, provider, ws, project_id, round_no,
                             result.warnings)
    if dispatch is None:
        return False
    for q in qs:
        key = q.slot.key
        raw = dispatch["answers"].get(key)
        if raw is None:
            continue
        attempts[key] = attempts.get(key, 0) + 1
        val = _dispatch_value(q, raw)
        if val is None:
            # UX-3：多选槽给逐项诊断（与 ask.py 同口径）
            bad = (split_multi_enum(q, raw)[1]
                   if q.slot.candidates_from == "enum" and getattr(q.slot, "multi", False)
                   else [])
            if attempts[key] >= _RETRY_LIMIT:
                io.notify(f"[提示] 「{q.slot.key}」多次未落合法值"
                          + (f"（未识别：{'、'.join(bad)}）" if bad else "")
                          + f"，本轮按推荐值；你的输入「{raw[:40]}」未采纳。")
                _fill_one_recommended(bp, q, ws, project_id, result,
                                      answered_keys, "dispatch.reject-limit")
            else:
                append_transcript(ws, project_id, "ask.reject", key=key,
                                  value=raw[:120], reason="value not valid")
                if bad:
                    io.notify(f"[提示] 「{q.slot.ask or q.slot.label}」未识别：{'、'.join(bad)}；"
                              f"合法候选：{q.candidates}（可直接报序号，如 1 3 5）。")
                else:
                    io.notify(f"[提示] 「{q.slot.ask or q.slot.label}」只能取 {q.candidates}，"
                              f"你这句「{raw[:30]}」已保留，可重答。")
            continue
        path = _apply_slot_value(bp, q.slot, val, "user", 1.0)
        if path is None:
            continue
        append_transcript(ws, project_id, "ask.answer", key=key, path=path,
                          value=val[:200], src="user", explicit=True)
        result.answered += 1
    added = _record_extras(ws, project_id, dispatch.get("extras", []))
    result.extras_seen += added
    return True


def _maybe_build(ws: Workspace, project_id: str, bp: Blueprint, provider,
                 io: AnswerIO, state: ForgeState, gaps: list[Any],
                 slots: list[Slot], max_calls: int | None,
                 result: ShellResult) -> None:
    """/build：required 缺口存在时先补齐推荐值并确认，再构建。"""
    # 有待审模块 → 构建必定被闸门拦下，直接给待处置清单，不再让用户白答一次 y
    _pending_now = sorted((load_review(ws, project_id).get("pending") or {}))
    if _pending_now:
        io.notify("[shell] 还有待审模块未处置，构建会被闸门拦下：")
        for m in _pending_now:
            io.notify(f"  - {m}（/review {m} 看全文 → /approve {m} 或 /revise {m} \"建议\"）")
        io.notify("  处置完再 /build。")
        return
    req = [g for g in gaps if g.slot.level == "required"]
    if req:
        io.notify(f"[shell] 仍有 {len(req)} 个 required 缺口：")
        for g in req:
            io.notify(f"  · {g.slot.key}（{g.slot.label}）")
    if req:
        confirm = io.confirm("以推荐值补齐剩余 required 缺口并开始构建？",
                             default=True)
    else:
        confirm = io.confirm("开始构建？", default=True)
    if not confirm:
        io.notify("[shell] 取消构建，回到会话。")
        return
    # 补齐 required 缺口（只补 required，recommended 留给后续轮）
    if req:
        from .ask import RoundQuestion as _RQ

        for g in req:
            sl = g.slot
            av = _default_of(_RQ(sl, [], sl.default or ""))
            path = _apply_slot_value(bp, sl, av.value, av.src,
                                     1.0 if av.explicit else 0.8)
            if path is None:
                continue
            append_transcript(ws, project_id, "ask.answer", key=sl.key,
                              path=path, value=av.value[:200], src=av.src,
                              explicit=av.explicit)
            result.answered += 1
        bp.save(ws, project_id)
    from .engine import build  # 延迟导入，避免顶层循环

    state.touch_stage(ws, project_id, "seeded")
    res = build(ws, project_id, provider=provider, max_calls=max_calls)
    result.warnings += res.warnings
    result.build_triggered = True
    for w in res.warnings:
        io.notify(f"  [warn] {w}")
    if res.gate_halted:
        io.notify("审核闸门：以下模块待处置后再续跑 build/resume（会话内命令）：")
        for m in res.pending_review:
            io.notify(f"  - {m}（/review {m} 看全文 → /approve {m} 通过 或 /revise {m} \"建议\"）")
        return
    io.notify(f"[shell] build done: calls={res.calls_used} 卷={res.volumes_written} "
              f"章={res.chapters_written}")
    if not res.ok:
        result.warnings.append("构建未完成，见上方 warnings")
        io.notify("构建未完成；`forge resume` 续跑，或继续本会话。")
    else:
        io.notify("构建完成。读章/修订建议链（P2/P3）将在后续版本接入本会话。")