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
    _render_bp_overview, _render_round, _RETRY_LIMIT, _settle_round)
from .io_console import AnswerIO, ConsoleIO
from .slots import Slot, default_slots, detect_gaps
from .state import Blueprint, ForgeState, append_transcript

_MAX_WINDOW = 3

HELP_TEXT = """会话命令：
  /exit         退出会话（未收敛缺口保留，退到 consulting 待 resume）
  /show         查看已填设定概览 + 补充设想登记状态
  /help         本帮助
  /build        触发构建（仍缺 required 时先补齐推荐值并确认再构建）
自由语（其余输入）：回答本轮缺口 / 补充设想（落不到槽位会登记 extras）。
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
    while True:
        gaps = detect_gaps(bp, slots)
        window = [g.slot for g in gaps
                  if g.slot.key not in answered_keys][:_MAX_WINDOW]

        if window:
            round_no += 1
            qs = _build_questions(bp, window, provider, round_no, ws, project_id,
                                  result.warnings, need_candidates=True)
            io.notify(_render_round(round_no, qs))
            io.notify("CR> 回车=取推荐值 / 自由语=回答或补充设想 / /help")
        else:
            qs = []
            io.notify("\n（本蓝图已无缺口：/build 构建，或自由语继续补充设想登记 extras）")

        line = io.ask_free("", "")
        if line is None:
            line = "/exit"

        cmd = line.strip().lower()
        # ---- 命令分发（确定性，无 LLM 控制权）----
        if cmd in ("/exit", "/quit", ":q"):
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
        if cmd in ("/help", "/h", "?", "help"):
            io.notify(HELP_TEXT)
            continue
        if cmd in ("/show", "@show", "show"):
            io.notify(_render_bp_overview(bp, slots) + _extras_banner(ws, project_id))
            continue
        if cmd in ("/build", "build"):
            _maybe_build(ws, project_id, bp, provider, io, state, gaps, slots,
                         max_calls, result)
            continue
        if cmd in ("/save", "save"):
            bp.save(ws, project_id)
            io.notify("[shell] 蓝图已保存。")
            continue

        # ---- 自由语（含空回车=取推荐值）----
        if not window:
            # 无缺口（或全部已答）：无法分派到槽，直接登记为补充设想
            if line.strip():
                added = _record_extras(ws, project_id, [line.strip()])
                result.extras_seen += added
                io.notify(f"[shell] 已登记 {added} 条补充设想（extras 待确认）。")
            continue
        if not line.strip():
            _settle_round(bp, qs, provider, io, ws, project_id, result,
                          answered_keys, attempts, round_no, "ask.default")
            bp.save(ws, project_id)
            result.rounds_done += 1
            continue
        ok = _dispatch_free_text(bp, qs, line, provider, ws, project_id,
                                 attempts, result, round_no, io, answered_keys)
        if not ok:
            _settle_round(bp, qs, provider, io, ws, project_id, result,
                          answered_keys, attempts, round_no, "dispatch.fallback")
        bp.save(ws, project_id)
        result.rounds_done += 1


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
            if attempts[key] >= _RETRY_LIMIT:
                io.notify(f"[提示] 「{q.slot.key}」多次未落合法值，本轮按推荐值。")
                _fill_one_recommended(bp, q, ws, project_id, result,
                                      answered_keys, "dispatch.reject-limit")
            else:
                append_transcript(ws, project_id, "ask.reject", key=key,
                                  value=raw[:120], reason="value not valid")
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
        io.notify("审核闸门：以下模块待处置后再续跑 build/resume：")
        for m in res.pending_review:
            io.notify(f"  - {m}（forge review {m} → approve / revise）")
        return
    io.notify(f"[shell] build done: calls={res.calls_used} 卷={res.volumes_written} "
              f"章={res.chapters_written}")
    if not res.ok:
        result.warnings.append("构建未完成，见上方 warnings")
        io.notify("构建未完成；`forge resume` 续跑，或继续本会话。")
    else:
        io.notify("构建完成。读章/修订建议链（P2/P3）将在后续版本接入本会话。")