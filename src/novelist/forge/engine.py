"""递归构建引擎（docs/10 §7）：DFS + 硬边界 + 增量落盘 + 续跑（M3l F1/F4）。

树形（docs/10 §7.1）：
```
book ── 设定骨架 + 卷主线一次出齐（写回蓝图 + 落 bible）
 ├─ worldview ─ system ─ setting_entry        ← F4 旁支（deepen=True）
 ├─ character_group ─ character               ← F4 旁支
 ├─ style / thread_set                        ← F4 旁支（叶）
 └─ volume 1..N ── 卷主线（vol≥2 细纲留 `forge roll`）
      └─ [vol=1] arc?（volume expand 时）─ chapter 1-K ─ beat?（chapter expand 时）
```
`deepen=False` 退化为 F1 最小树（book → volume → chapter），存量行为不变。

硬边界（docs/10 §7.3）：max_calls 分阶段配额（显式 `--max-calls` 优先；否则 build/seed/shell
按规模推导 `12 + N×3 + K×2 + min(M,24)`（ADR-033 B，下限 12），`resume` 硬默认 60、`roll` 每卷 40）；
max_depth=4（book=0 … beat=4，超深强制 done）；max_width=4（children 截断）；
每节点 max_retries=1（解析失败重试一次，再失败回退父层产物）。

续跑：chapter/volume 沿用产物存在性判据；旁支/arc/beat 用 `nodes/<node_id>.json`
落盘判据（docs/10 §7.4 增量落盘）。SIGINT 在当前节点后落盘退出。
"""

from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..core.llm import ModerationBlockedError
from ..core.output import emit, heartbeat
from ..storage.workspace import Workspace
from . import genres as _genres
from .nodes import (CHILD_KIND, LEAF_KINDS, NodeContext, chapter_range_of, run_node,
                    sync_bible, synthesize_worldstate, _load_json_list)
from .covenant import affected_modules, build_covenant, touched_entries
from .review import (REVIEW_MODULES, load_review, mark_pending, pending_modules,
                     stage_pending_for_revise)
from .state import Blueprint, ForgeState, append_transcript


@dataclass
class BuildResult:
    ok: bool
    project_id: str
    calls_used: int = 0
    nodes_done: int = 0
    nodes_failed: int = 0   # 重试后仍失败的节点（改动已回滚）
    chapters_written: int = 0
    volumes_written: int = 0
    budget_exhausted: bool = False
    budget_limit: int = 60
    warnings: list[str] = field(default_factory=list)
    interrupted: bool = False
    blueprint_path: str = ""
    gate_halted: bool = False
    pending_review: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "calls_used": self.calls_used,
            "nodes_done": self.nodes_done,
            "nodes_failed": self.nodes_failed,
            "chapters_written": self.chapters_written,
            "volumes_written": self.volumes_written,
            "budget_exhausted": self.budget_exhausted,
            "budget_limit": self.budget_limit,
            "interrupted": self.interrupted,
        }


def _pack_for_bp(bp: Blueprint) -> dict:
    meta = bp.get("meta") or {}
    template = meta.get("template") or meta.get("genre") or ""
    try:
        return _genres.load_pack_for(template) if template else _genres.load_pack(_genres.GENERIC_ID)
    except KeyError:
        return _genres.load_pack(_genres.GENERIC_ID)


def _node_label(kind: str, vol: int = 0, ch: int = 0, child: dict | None = None) -> str:
    cid = str((child or {}).get("id") or (child or {}).get("brief") or "")[:12]
    if kind == "chapter":
        return f"L3 chapter {vol}-{ch}"
    if kind == "volume":
        return f"L1 volume {vol}"
    if kind == "beat":
        return f"L4 beat {vol}-{ch}"
    if kind == "arc":
        return f"L2 arc {vol} {cid}"
    depth = {"book": "L0", "worldview": "L1", "character_group": "L1",
             "style": "L1", "thread_set": "L1", "system": "L2",
             "character": "L2", "setting_entry": "L3"}.get(kind, "L?")
    return f"{depth} {kind} {cid}".rstrip()


def _side_node_id(kind: str, child: dict | None) -> str:
    if kind in ("worldview", "character_group", "style", "thread_set"):
        return kind
    cid = str((child or {}).get("id") or "").strip()
    return f"{kind}:{cid}" if cid else kind


def _persist_node(ws: Workspace, project_id: str, node_id: str, res: Any) -> None:
    """节点产物落盘 nodes/<node_id>.json（docs/10 §7.4 增量落盘；resume 判据）。

    node_id 含 `:`（如 arc:2:arc-1）——Windows 文件名不允许，净化为 `-`。
    """
    if res is None:
        return
    d = ws._abs(f"{project_id}/workspace/forge/nodes")  # noqa: SLF001
    d.mkdir(parents=True, exist_ok=True)
    payload = {"kind": res.kind, "node_id": res.node_id, "decide": res.decide,
               "reason": res.reason, "artifact": res.artifact}
    (d / (node_id.replace(":", "-") + ".json")).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class _GateHalt(Exception):
    """审核闸门暂停：携带本轮落 pending 的模块（走 try/finally 正常收尾）。"""

    def __init__(self, modules: list[str]):
        super().__init__(", ".join(modules))
        self.modules = modules


# 写入可见性（2026-09-19 拍板：对齐编码 agent——写完文件要播报写了什么）。
# 节点完成后除耗时行外补一行产物增量：蓝图各段计数前后 diff（确定性，零 LLM）。
_DELTA_LIST_SECTIONS = ("characters", "threads", "lines", "volumes", "chapters",
                        "locations", "items", "skills", "settings")
_DELTA_DICT_SECTIONS = ("worldview", "style", "meta")


def _bp_counts(bp) -> dict[str, int]:
    counts = {s: len(bp.section(s) or []) for s in _DELTA_LIST_SECTIONS}
    import json as _json

    for s in _DELTA_DICT_SECTIONS:
        v = bp.get(s)
        counts[s] = len(_json.dumps(v or {}, ensure_ascii=False, sort_keys=True))
    return counts


def _delta_note(before: dict[str, int], after: dict[str, int]) -> str:
    """蓝图计数 diff → 一行产物摘要；无变化返回空串。"""
    parts: list[str] = []
    labels = {"characters": "人物", "threads": "伏笔", "lines": "线索", "volumes": "卷纲",
              "chapters": "章细纲", "locations": "地点", "items": "物品", "skills": "技能",
              "settings": "设定", "worldview": "世界观", "style": "文风", "meta": "meta"}
    for s in _DELTA_LIST_SECTIONS:
        d = after[s] - before[s]
        if d > 0:
            parts.append(f"+{d} {labels[s]}")
    for s in _DELTA_DICT_SECTIONS:
        if after[s] != before[s]:
            parts.append(f"{labels[s]}已更新")
    return "｜" + " ".join(parts) if parts else ""


def _log_round_summary(log, *, kind: str, calls_used: int, max_calls: int,
                       nodes_done: int, nodes_failed: int, elapsed_s: float,
                       gate_halted: bool, budget_exhausted: bool,
                       interrupted: bool) -> None:
    """一轮构建的收尾摘要（2026-09-16 UX）：成功/回退/中止三态与预算消耗一次说清。

    此前进度行只有 `[10/234] <节点> … ok`（把**调用预算**当成进度）+ 末尾一堆 warn，
    用户看不出"这次到底成不成、有几个节点没生成"。
    """
    from ..core.output import fmt_duration

    zt = ("闸门暂停（待审核）" if gate_halted else
          "预算耗尽" if budget_exhausted else
          "已中止（连续失败过多）" if interrupted else "完成")
    log(f"[summary] {kind}结束：{zt} · 调用 {calls_used}/{max_calls} · "
        f"节点 ok {nodes_done} / 回退 {nodes_failed} · 用时 {fmt_duration(elapsed_s)}")
    if nodes_failed:
        log(f"          回退的 {nodes_failed} 个节点未生成新产物，沿用上一次可用内容"
            "（节点改动已回滚，不会留下半成品）；如需还原更早状态："
            "`forge snapshots` 查快照 + `forge rollback --to <name>`")


class _FailAbort(Exception):
    """连续节点失败达阈值 → 中止本轮构建（docs/10 §12）。"""


class _FailStreak:
    """连续失败计数（docs/10 §12：连续 3 个节点解析失败 → 中止并提示换 provider）。

    此前该规则**完全没实现**：实测 4 个节点（book / character_group / thread_set 等）
    连续失败后构建仍继续跑，用户只在末尾看到一堆 warn。
    """

    LIMIT = 3

    def __init__(self) -> None:
        self.n = 0

    def reset(self) -> None:
        self.n = 0

    def bump(self) -> int:
        self.n += 1
        return self.n


def _one_line(e: BaseException, width: int = 200) -> str:
    """把异常压成单行（JSON 诊断带多行摘录，warn 里必须折起来）。"""
    return f"{type(e).__name__}: {str(e).replace(chr(10), ' ⏎ ')[:width]}"


def _retry_hint(label: str, e: BaseException) -> str:
    """重试 prompt 携带的诊断（2026-09-16）：模型必须看到自己上次错在哪。

    此前 `extra_warnings` 只进返回的 warns 列表、**不进 prompt**，重试等于盲发同一请求
    （实测 thread_set 连续两轮同一 JSON 语法错）。
    """
    body = str(e)
    if len(body) > 900:
        body = body[:900] + "…"
    return f"{label} 上一轮输出不可用：{body}"


def _fail_note(label: str, e: BaseException) -> str:
    """节点失败的留痕文案：说清**真实后果**（改动已回滚、沿用上一次可用产物）。

    旧文案「回退父层产物」与实际行为不符——当时只跳过节点文件落盘，蓝图/ bible 里
    已经写入的半成品照样 `bp.save()` 落盘（2026-09-16 事故）。
    """
    return (f"{label}: 重试仍失败 → 已回滚该节点的改动，沿用上一次可用产物；"
            f"原因：{_one_line(e)}")


def _maybe_abort(streak: "_FailStreak", label: str) -> None:
    if streak.bump() >= streak.LIMIT:
        raise _FailAbort(
            f"连续 {streak.LIMIT} 个节点重试后仍失败（最近：{label}）→ 已中止本轮构建。"
            "多为 provider 输出不稳定 / 模型不适合结构化 JSON：换 provider 或降规模后 "
            "`forge resume` 续跑；怀疑产物被写脏时用 `forge snapshots` + `forge rollback`")


def _node_done(ws: Workspace, project_id: str, node_id: str) -> bool:
    fname = node_id.replace(":", "-") + ".json"
    return ws._abs(f"{project_id}/workspace/forge/nodes/{fname}").exists()  # noqa: SLF001


def _build_call_budget(n_volumes: int, k_per_volume: int, n_characters: int,
                       override: int | None) -> int:
    """ADR-033 B：调用预算按规模推导（max_calls 不显式给出时）。

    量级估算（每项一次 provider 调用上下）：书 1 + 卷主线 N + 卷1 章节 K*2
    （每章细纲 ~1 次、可含 beat/审查） + 设定/体系 ~8 + 人物取 min(M, 24)。
    `override`（`--max-calls`）非空则直接采用，不再推导。
    """
    if override is not None:
        return int(override)
    return max(12 + n_volumes * 3 + k_per_volume * 2 + min(n_characters, 24), 12)


# ADR-033 A：合法长尾的"子 kind"（worldview→system、system→setting_entry、
# character_group→character）可享有更大宽度；条目型（volume→arc、chapter→beat）
# 维持 `max_width`。子类型按父 kind 经 CHILD_KIND 判定。
_WIDE_TARGETS = {"character", "system", "setting_entry"}


def _child_width(parent_kind: str, max_width: int, max_width_list: int) -> int:
    target = CHILD_KIND.get(parent_kind)
    return max_width_list if target in _WIDE_TARGETS else max_width


def build(ws: Workspace, project_id: str, *, provider: Any, **kw: Any) -> "BuildResult":
    """全权构建（薄包装）：给原始调用日志挂 forge:build 上下文。"""
    from ..core.calllog import call_context

    with call_context("forge:build"):
        return _build_impl(ws, project_id, provider=provider, **kw)


def _build_impl(ws: Workspace, project_id: str, *, provider,
          max_calls: int | None = None, max_depth: int = 4, max_width: int = 4,
          max_width_list: int = 12,  # ADR-033 A：列表型长尾（character/system/setting_entry）宽度上限
          spec: Any = None, pack: dict | None = None,
          resume: bool = False, deepen: bool = True,
          gate: bool = True,
          coherence_review: bool = False,
          doctor: bool = True,
          log_fn: Callable[[str], None] | None = None) -> BuildResult:
    """全权构建：book → 旁支 DFS（deepen）→ volume(全卷) → arc?/chapter(仅 vol=1)/beat?，
    末尾 worldstate 确定性合成。

    `resume=True`：跳过已落盘节点（幂等续跑）。`deepen=False` 退化为 F1 最小树。
    `gate=False`：关闭审核闸门（ADR-024；单测/脚本直跑用，CLI 默认开）。
    `coherence_review=True`：细纲连读审查（方案 A）——每章落盘后 ≥2 章触发一次
    LLM 连读并落 findings。默认关：审查消耗 provider 调用（会计入调用预算，
    且脚本化 fake 的测试对其调用序敏感），由 harness/CLI 显式开启。
    `log_fn` 缺省打印进度行 `[calls/max] <node> … ok (calls=N)`。
    """
    warnings: list[str] = []
    log = log_fn or emit
    bp = Blueprint.load(ws, project_id)
    # 存量污染名治愈（ask 槽位直写时代的遗留；textnorm 与 ask/nodes 共用规则）
    from .textnorm import heal_blueprint_characters

    for w in heal_blueprint_characters(bp):
        warnings.append(w)
        log(f"[heal] {w}")
    if warnings:
        bp.save(ws, project_id)
    meta = bp.get("meta") or {}
    scale = meta.get("scale")
    if not scale:
        raise ValueError(f"{project_id}: 蓝图缺 meta.scale——先跑 `forge seed`")
    N = int(scale.get("volumes", 3))
    K = int(scale.get("chapters_per_volume", 20))
    # ADR-033 B：未显式给 --max-calls 时按 N/K/M 推导预算（不再固定 60）
    n_chars = len(bp.get("characters") or []) or 8
    max_calls = _build_call_budget(N, K, n_chars, max_calls)
    pack = pack or _pack_for_bp(bp)
    state = ForgeState.load(ws, project_id)
    state.stage = "build"
    calls_used = int(state.calls_used or 0)

    # ---- 审核闸门（ADR-024）：有待审模块 → 不消耗任何调用，直接交还用户 ----
    pending_now = sorted(pending_modules(ws, project_id)) if gate else []
    if pending_now:
        for m in pending_now:
            log(f"[gate] {REVIEW_MODULES[m]['label']}({m}) 待审核："
                f"forge review {m} → approve / revise")
        return BuildResult(ok=True, project_id=project_id,
                           calls_used=calls_used, nodes_done=0,
                           gate_halted=True, pending_review=pending_now,
                           warnings=["审核闸门拦截：先处置 pending 模块再 build"])
    gate_halted = False

    def _gated(kind: str) -> list[str]:
        """该节点产出中、开关为开的审核模块（gate=False 时恒空）。"""
        if not gate:
            return []
        cfg = load_review(ws, project_id)
        return [m for m, spec in REVIEW_MODULES.items()
                if spec["kind"] == kind and cfg["switches"].get(m, True)]

    def _count_hook() -> None:
        """G2 修复（2026-09-05）：连读/蓝图审查的 provider 调用计入调用预算。

        原先审查直接 complete 绕过 calls_used 计量——预算与 transcript 双失真。
        """
        nonlocal calls_used
        calls_used += 1

    # 构建前置文件快照（docs/10 §7.6 F5）：rollback / --diff 的基线
    from .snapshot import take_snapshot

    snap = take_snapshot(ws, project_id, label="build")
    log(f"[snapshot] {snap.name}")

    nodes_done = 0
    nodes_failed = 0   # 重试后仍失败（改动已回滚、沿用上一次可用产物）的节点数
    streak = _FailStreak()
    chapters_written = 0
    volumes_written = 0
    budget_exhausted = False
    interrupted = False
    start = time.time()

    append_transcript(ws, project_id, "build.start", rev=bp.data["rev"], scale=scale,
                      max_calls=max_calls, resume=resume)

    # 骨架先落盘（seed 已有的 worldview/characters/style/threads → bible，AG1 前置）
    sync_bible(ws, project_id, bp)

    def _budget_check() -> bool:
        nonlocal budget_exhausted
        if calls_used >= max_calls:
            budget_exhausted = True
            return False
        return True

    def _call(ctx: NodeContext, kind: str, depth: int, vol: int = 0, ch: int = 0,
              child: dict | None = None):
        """执行一个节点（含 retry=1 与降级兜底）。返回 NodeResult | None（预算耗尽/失败跳过）。"""
        nonlocal calls_used, nodes_done, nodes_failed
        label = _node_label(kind, vol, ch, child)
        if depth > max_depth:
            warnings.append(f"{label}: 超过 max_depth={max_depth}，按 done 处理")
            return None
        if not _budget_check():
            warnings.append(f"{label}: 预算耗尽（{calls_used}/{max_calls}），未生成")
            return None
        started = calls_used + 1
        # UX-2（2026-09-19）：调用**开始前**先报"生成中"，长调用挂心跳——
        # 此前只有完成行，book 节点实测 17–21s 零输出，用户无从判断在跑还是卡死。
        log(f"[{started}/{max_calls} 调用] {label} … 生成中")
        counts_before = _bp_counts(bp)
        try:
            with heartbeat(label):
                res = run_node(ctx, kind)
            calls_used = started
            nodes_done += 1
            streak.reset()
            ctx.retry_hint = ""
            log(f"[{calls_used}/{max_calls} 调用] {label} … ok ({time.time() - start:.1f}s)"
                + _delta_note(counts_before, _bp_counts(bp)))
            for w in res.warnings:
                warnings.append(f"{label}: {w}")
            append_transcript(ws, project_id, "build_node", kind=kind, node=label,
                              decide=res.decide, reason=(res.reason or "")[:120],
                              tokens_in=res.tokens_in, tokens_out=res.tokens_out)
            return res
        except ValueError as e:  # 解析/校验失败 → retry 1（docs/10 §12）
            ctx.retry_hint = _retry_hint(label, e)  # 重试前把诊断交给模型
            if not _budget_check():
                nodes_failed += 1
                warnings.append(f"{label}: 重试时预算耗尽，未生成（沿用上一次可用产物）")
                _maybe_abort(streak, label)
                return None
            ctx.extra_warnings.append(f"{label}: 首次失败（{_one_line(e)}），重试中")
            try:
                res2 = run_node(ctx, kind)
                calls_used = started + 1
                nodes_done += 1
                streak.reset()
                ctx.retry_hint = ""
                log(f"[{calls_used}/{max_calls} 调用] {label} … ok (retry, "
                    f"{time.time() - start:.1f}s)")
                for w in res2.warnings:
                    warnings.append(f"{label}: {w}")
                append_transcript(ws, project_id, "build_node", kind=kind, node=label,
                                  decide=res2.decide, reason=(res2.reason or "")[:120],
                                  tokens_in=res2.tokens_in, tokens_out=res2.tokens_out,
                                  retried=True)
                return res2
            except (ValueError, ModerationBlockedError) as e2:  # noqa: BLE001
                calls_used = started + 1
                nodes_failed += 1
                warnings.append(_fail_note(label, e2))
                _maybe_abort(streak, label)
                return None
        except ModerationBlockedError as e:
            calls_used = started
            nodes_failed += 1
            warnings.append(f"{label}: 审核拦截（{_one_line(e)}），该节点标记 blocked、人工补")
            _maybe_abort(streak, label)
            return None
        except Exception as e:  # noqa: BLE001 - 节点级失败不拖垮整棵树
            calls_used = started
            nodes_failed += 1
            warnings.append(_fail_note(label, e))
            _maybe_abort(streak, label)
            return None

    try:
        # ---- L0 book ----
        book_done = bool(bp.section("volumes")) if resume else False
        if not book_done:
            ctx = NodeContext(ws=ws, project_id=project_id, bp=bp, provider=provider,
                              pack=pack, spec=spec)
            res = _call(ctx, "book", 0)
            if res is None and not budget_exhausted:
                # 根节点失败即中止（2026-09-16 拍板）：book 是整棵树的根，
                # 它没生成而 L1/L2 照跑，就是"新内容 + 旧骨架"的混血产物
                # （真机事故：volume-1/style 用新内容，lines/lines 骨架仍是旧的，
                #  同一批产物里女主出现苏嫣然/苏晓/苏沐三种写法）。
                raise _FailAbort(
                    "L0 book 未生成（根节点失败）→ 已中止本轮构建：后续节点的骨架都依赖它，"
                    "继续跑只会产出「新内容 + 旧骨架」的混血产物。"
                    "产物改动已回滚；换 provider 或降规模后 `forge resume` 重跑，"
                    "也可 `forge rollback` 回到本轮构建前的快照")
            _persist_node(ws, project_id, "book", res)
            bp.save(ws, project_id)
            sync_bible(ws, project_id, bp)
            # 审核闸门：book 产出模块按开关落 pending（ADR-024）
            gated = _gated("book") if res is not None and res.ok else []
            if gated:
                mark_pending(ws, project_id, bp, gated, log_fn=log)
                raise _GateHalt(gated)

        # ---- F4 旁支 DFS（docs/10 §7.1：worldview/character_group/style/thread_set）----
        # 次序（自定决策）：设定 → 人物 → 文风 → 伏笔，伏笔最后可引用前面产出
        if deepen:
            def _branch(kind: str, depth: int, child: dict | None = None) -> None:
                ck = CHILD_KIND.get(kind)
                leaf = kind in LEAF_KINDS
                nid = _side_node_id(kind, child)
                if resume and _node_done(ws, project_id, nid):
                    log(f"[skip] {nid}（nodes/ 已有产物）")
                    return
                ctx = NodeContext(ws=ws, project_id=project_id, bp=bp, provider=provider,
                                  pack=pack, spec=spec, child=child)
                res = _call(ctx, kind, depth, child=child)
                _persist_node(ws, project_id, nid, res)
                if res is None or not (res.decide == "expand" and res.children):
                    return
                if ck is None or leaf or depth >= max_depth:
                    return
                width = _child_width(kind, max_width, max_width_list)
                if len(res.children) > width:
                    warnings.append(f"{nid}: children {len(res.children)} 个超出 max_width={width}，截断")
                for i, raw in enumerate(res.children[:width], 1):
                    item = raw if isinstance(raw, dict) else {"brief": str(raw)}
                    item.setdefault("id", f"{ck}{i}")
                    _branch(ck, depth + 1, child=item)

            for kind in ("worldview", "character_group", "style", "thread_set"):
                if budget_exhausted:
                    warnings.append(f"{kind}: 预算耗尽，旁支未深化")
                    break
                _branch(kind, 1)
                bp.save(ws, project_id)
                sync_bible(ws, project_id, bp)

            # 类型包声明式扩展节点（2026-09-19 拍板）：pack.extra_node_kinds 里声明、
            # 蓝图中尚无产物的扩展节点，作为 book 旁支叶节点跑掉（白名单外 kind 在
            # nodes 层硬拒；resume 时靠 nodes/ 产物判存在性跳过）。
            for spec in (pack or {}).get("extra_node_kinds") or []:
                kind = str((spec or {}).get("kind") or "")
                if not kind:
                    continue
                if budget_exhausted:
                    warnings.append(f"{kind}: 预算耗尽，扩展节点未生成")
                    break
                if bp.section(str(spec.get("section") or "")) and resume:
                    continue  # 续跑且目标段已有内容 → 跳过（幂等）
                _branch(kind, 1)
                bp.save(ws, project_id)
                sync_bible(ws, project_id, bp)

        # ---- 蓝图连读审查（批次三·方案1，opt-in）：卷展开前全局顺一遍 ----
        # book+旁支刚出齐、正文未动笔——此刻的自相矛盾/人物撞型污染整个下游，
        # 是"交给 AI 顺一遍"价值最高的引入点。失败静默跳过，不阻断 build。
        if coherence_review:
            try:
                from .coherence import run_blueprint_review, BP_FINDINGS_REL

                if not ws._abs(f"{project_id}/{BP_FINDINGS_REL}").exists():  # noqa: SLF001
                    fnd = run_blueprint_review(ws, project_id, bp, provider,
                                               count_hook=_count_hook)
                    if fnd:
                        nc = len(fnd.get("contradictions") or [])
                        nk = len(fnd.get("character_conflicts") or [])
                        log(f"[coherence] 蓝图连读：矛盾{nc} 撞型{nk} "
                            f"注意{len(fnd.get('must_watch') or [])}")
            except Exception as e:  # noqa: BLE001
                warnings.append(f"blueprint review: {type(e).__name__}: {e}"[:120])

        # ---- L1 volume（全部卷，卷主线一次出齐）----
        existing_vols = _load_json_list(ws, project_id, "outline/volumes.json")
        for vol in range(1, N + 1):
            if budget_exhausted:
                warnings.append(f"volume {vol}: 预算耗尽，未生成（卷主线不完整）")
                break
            vol_arcs: list[dict] = []
            if resume and any(x.get("vol") == vol for x in existing_vols):
                # 已落盘 → 跳过，但确保蓝图有该卷条目（续跑一致性）
                row = next(x for x in existing_vols if x.get("vol") == vol)
                _upsert_volume_from_row(bp, row)
            else:
                ctx = NodeContext(ws=ws, project_id=project_id, bp=bp, provider=provider,
                                  pack=pack, spec=spec, vol=vol)
                res = _call(ctx, "volume", 1, vol=vol)
                _persist_node(ws, project_id, f"volume:{vol}", res)
                if res is not None and res.ok:
                    volumes_written += 1
                    gated_v = _gated("volume")
                    if gated_v:
                        mark_pending(ws, project_id, bp, gated_v, log_fn=log,
                                     vol=vol)
                        raise _GateHalt(gated_v)
                bp.save(ws, project_id)
                # F4b：volume expand → arc 节点（章段小弧，≤max_width）
                if deepen and res is not None and res.decide == "expand" and res.children:
                    ck = CHILD_KIND["volume"]
                    if len(res.children) > max_width:
                        warnings.append(f"volume {vol}: children {len(res.children)} 个超出 max_width={max_width}，arc 截断")
                    for i, raw in enumerate(res.children[:max_width], 1):
                        item = raw if isinstance(raw, dict) else {"brief": str(raw)}
                        item.setdefault("id", f"{ck}{i}")
                        ctx_a = NodeContext(ws=ws, project_id=project_id, bp=bp, provider=provider,
                                            pack=pack, vol=vol, child=item)
                        res_a = _call(ctx_a, "arc", 2, vol=vol, child=item)
                        _persist_node(ws, project_id, f"arc:{vol}:{item['id']}", res_a)
                        if budget_exhausted:
                            break
                    bp.save(ws, project_id)
            # 本卷 arcs（outline/arcs.json，供 chapter 注入）
            vol_arcs = [x for x in _load_json_list(ws, project_id, "outline/arcs.json")
                        if int(x.get("vol") or 0) == vol]
            # 卷闸门（docs/10 §7.1 B2）：仅 vol=1 展开 chapter
            if vol != 1:
                continue
            for ch in range(1, K + 1):
                if budget_exhausted:
                    warnings.append(f"chapter 1-{ch}: 预算耗尽，未生成（剩余 {K - ch + 1} 章）")
                    break
                gist_path = ws.outline_chapter_path(project_id, 1, ch)
                if resume and gist_path.exists():
                    continue
                prev = None
                if ch > 1:
                    from ..core.bible import parse_gist

                    prev = parse_gist(ws, project_id, 1, ch - 1)
                ctx_ch = NodeContext(ws=ws, project_id=project_id, bp=bp, provider=provider,
                                     pack=pack, spec=spec, vol=1, ch=ch, prev_gist=prev,
                                     arcs=vol_arcs or None)
                # G4 修复（2026-09-05）：连读审查禁令注入本章细纲 prompt（细纲层
                # 也规避母题重复，而不是等问题固化到账本、正文期才拦）。
                if coherence_review and ch >= 2:
                    try:
                        from .coherence import load_coherence_bans

                        ctx_ch.extra["coherence_bans"] = load_coherence_bans(
                            ws, project_id, 1, ch)
                    except Exception:  # noqa: BLE001
                        ctx_ch.extra["coherence_bans"] = []
                res_ch = _call(ctx_ch, "chapter", 2, vol=1, ch=ch)
                if res_ch is not None and res_ch.ok:
                    chapters_written += 1
                    gated_c = _gated("chapter")
                    if gated_c:
                        mark_pending(ws, project_id, bp, gated_c, log_fn=log,
                                     vol=1, ch=ch)
                        raise _GateHalt(gated_c)
                    # ---- 细纲连读审查（方案 A，用户 2026-09-05）：opt-in，≥2 章触发 ----
                    # 失败/单章静默跳过；findings 落盘后由正文生成侧读取注入禁令。
                    if coherence_review and ch >= 2:
                        try:
                            from .coherence import run_coherence_review

                            fnd = run_coherence_review(ws, project_id, bp, provider, 1, ch,
                                                       count_hook=_count_hook)
                            if fnd:
                                nm = len(fnd.get("motif_repeats") or [])
                                nc = len(fnd.get("causal_issues") or [])
                                log(f"[coherence] 1-{ch} 连读审查：母题重复{nm} 因果{nc}")
                        except Exception as e:  # noqa: BLE001
                            warnings.append(f"coherence 1-{ch}: {type(e).__name__}: {e}"[:120])
                bp.save(ws, project_id)
                # F4b：chapter expand → beat 节点（重场戏拍级提示，每章至多 1 次）
                if deepen and res_ch is not None and res_ch.ok \
                        and res_ch.decide == "expand" and res_ch.children:
                    briefs = "; ".join(
                        str((c if isinstance(c, dict) else {"brief": str(c)}).get("brief") or "")
                        for c in res_ch.children[:max_width]).strip("; ")
                    ctx_b = NodeContext(ws=ws, project_id=project_id, bp=bp, provider=provider,
                                        pack=pack, vol=1, ch=ch,
                                        child={"id": f"beat{ch}", "brief": briefs})
                    res_b = _call(ctx_b, "beat", 3, vol=1, ch=ch,
                                  child=ctx_b.child)
                    _persist_node(ws, project_id, f"beat:1:{ch}", res_b)
                    bp.save(ws, project_id)
    except _GateHalt as g:
        gate_halted = True
        warnings.append(f"审核闸门暂停（待处置：{g.modules}）——build/resume 在处置后可续跑")
    except _FailAbort as a:
        interrupted = True
        warnings.append(str(a))
    except KeyboardInterrupt:
        interrupted = True
        warnings.append("SIGINT：当前节点后落盘退出（被中断节点未落盘、calls_used 不回退）")
    finally:
        # ---- 末尾：worldstate 确定性合成（零 LLM，docs/10 §7.5）----
        # F1 修复（2026-09-05）：仅在 worldstate **不存在**时初始化合成——
        # 原先无条件重写会把正文期推进的 time.now / 人物死亡受伤位置 / 运行态
        # pending 全部抹回蓝图初始态（排查 P0-F1）。roll 的日程登记走独立路径。
        _ws_path = ws.bible_path(project_id, "worldstate")
        if not _ws_path.exists():
            wstate = synthesize_worldstate(bp)
            ws.write_json(_ws_path, wstate)
        bp.save(ws, project_id)
        sync_bible(ws, project_id, bp)
        state.calls_used = calls_used
        state.stage = ("review" if gate_halted
                       else "built" if not interrupted else "build")
        state.save(ws, project_id)
        append_transcript(ws, project_id, "build.end",
                          calls_used=calls_used, nodes_done=nodes_done,
                          chapters_written=chapters_written,
                          budget_exhausted=budget_exhausted,
                          interrupted=interrupted, gate_halted=gate_halted,
                          warnings=warnings[:10])
        # 待裁决结构冲突计数（2026-09-16 拍板：主线/伏笔冲突交用户裁决，不静默改结构）
        try:
            from .conflicts import open_conflicts

            _n_cf = len(open_conflicts(ws, project_id))
            if _n_cf:
                log(f"[conflicts] {_n_cf} 条待裁决结构冲突（第二条主线 / 归一后同名伏笔）——"
                    "`forge conflicts` 查看，`forge conflicts --resolve <id> <choice>` 裁决；"
                    "未裁决时蓝图只保留先出现的那条")
        except Exception as e:  # noqa: BLE001 - 计数失败不影响构建收尾
            warnings.append(f"待裁决冲突计数失败：{_one_line(e)}")
        # 蓝图体检（2026-09-19 拍板，档 1 agent 化）：构建末尾自动跑只读体检——
        # 确定性预检 + 证据环 LLM 审查，报告落 workspace/forge/doctor.md。
        # 软失败、不写蓝图/bible、中断时不跑。
        if doctor and not interrupted:
            try:
                from .doctor import run_doctor

                run_doctor(ws, project_id, bp, provider, log=log)
            except Exception as e:  # noqa: BLE001 - 体检绝不阻断构建收尾
                warnings.append(f"doctor 体检失败：{_one_line(e)}")
        # 结果快照（F5e diff 基线 / 默认 rollback 点）：build 完成后的产物状态
        from .snapshot import take_snapshot

        take_snapshot(ws, project_id, label="build-ok")

    _log_round_summary(log, kind="构建", calls_used=calls_used, max_calls=max_calls,
                       nodes_done=nodes_done, nodes_failed=nodes_failed,
                       elapsed_s=time.time() - start, gate_halted=gate_halted,
                       budget_exhausted=budget_exhausted, interrupted=interrupted)
    ok = (not interrupted and not budget_exhausted
          and (chapters_written > 0 or gate_halted))
    return BuildResult(
        ok=ok,
        project_id=project_id,
        calls_used=calls_used,
        nodes_done=nodes_done,
        nodes_failed=nodes_failed,
        chapters_written=chapters_written,
        volumes_written=volumes_written,
        budget_exhausted=budget_exhausted,
        budget_limit=max_calls,
        warnings=warnings,
        interrupted=interrupted,
        gate_halted=gate_halted,
        pending_review=sorted(pending_modules(ws, project_id)) if gate_halted else [],
        blueprint_path=str(ws._abs(f"{project_id}/workspace/forge/blueprint.json")),  # noqa: SLF001
    )


# ---- 审核 revise（ADR-024）：按用户建议重生成模块内容，重新落 pending ----
def revise_module(ws: Workspace, project_id: str, module: str, suggestions: str,
                  provider, *, log_fn: Callable[[str], None] | None = None) -> list[str]:
    """重生成待审模块内容（book 模块定向段 / 大纲模块节点重跑）。

    返回差异说明行（如实列出新旧矛盾；正文风险由调用方提示）。重生成后仍 pending。
    """
    from .nodes import revise_book_section

    spec = REVIEW_MODULES.get(module)
    if spec is None:
        raise ValueError(f"unknown review module: {module}")
    log = log_fn or emit
    cfg = load_review(ws, project_id)
    entry = cfg["pending"].get(module)
    if entry is None:
        raise ValueError(f"{module}: 无待审内容（先触发该模块生成）")
    vol = int(entry.get("vol") or 0)
    ch = int(entry.get("ch") or 0)
    stage_pending_for_revise(ws, project_id, module, suggestions)
    bp = Blueprint.load(ws, project_id)
    log(f"[gate] revise {module}（{spec['label']}）… 重生成中")

    if spec["kind"] == "book":
        _, diffs = revise_book_section(provider, bp, module, suggestions)
        bp.save(ws, project_id)
        sync_bible(ws, project_id, bp)
        mark_pending(ws, project_id, bp, [module], log_fn=log)
        return diffs

    # ---- 大纲模块：删旧产物 → 带 extra_instruction 重跑节点（retry=1 同 build）----
    def _run(kind: str, ctx: NodeContext):
        try:
            return run_node(ctx, kind)
        except ValueError as e:
            ctx.extra_instruction = f"{ctx.extra_instruction}\n（上次输出解析失败：{e}，请修正格式）"
            return run_node(ctx, kind)

    if spec["kind"] == "volume":
        if not vol:
            raise ValueError(f"{module}: pending 缺 vol")
        old_row = next((x for x in bp.data.get("volumes") or []
                        if int(x.get("vol") or 0) == vol), None)
        nodes_d = ws._abs(f"{project_id}/workspace/forge/nodes")  # noqa: SLF001
        (nodes_d / f"volume-{vol}.json").unlink(missing_ok=True)
        ctx = NodeContext(ws=ws, project_id=project_id, bp=bp, provider=provider,
                          pack=_pack_for_bp(bp), vol=vol,
                          extra_instruction=suggestions)
        res = _run("volume", ctx)
        _persist_node(ws, project_id, f"volume:{vol}", res)
        bp.save(ws, project_id)
        sync_bible(ws, project_id, bp)
        mark_pending(ws, project_id, bp, [module], log_fn=log, vol=vol)
        new_row = next((x for x in bp.data.get("volumes") or []
                        if int(x.get("vol") or 0) == vol), None)
        return diff_rows(old_row, new_row, f"volumes[vol={vol}]")

    # chapter：旧细纲全文 diff（文本 unified diff）
    if not (vol and ch):
        raise ValueError(f"{module}: pending 缺 vol/ch")
    gist_path = ws.outline_chapter_path(project_id, vol, ch)
    old_text = gist_path.read_text(encoding="utf-8") if gist_path.exists() else ""
    nodes_d = ws._abs(f"{project_id}/workspace/forge/nodes")  # noqa: SLF001
    (nodes_d / f"chapter-{vol}-{ch}.json").unlink(missing_ok=True)
    prev = None
    if ch > 1:
        from ..core.bible import parse_gist

        prev = parse_gist(ws, project_id, vol, ch - 1)
    arcs = [x for x in _load_json_list(ws, project_id, "outline/arcs.json")
            if int(x.get("vol") or 0) == vol]
    ctx = NodeContext(ws=ws, project_id=project_id, bp=bp, provider=provider,
                      pack=_pack_for_bp(bp), vol=vol, ch=ch, prev_gist=prev,
                      arcs=arcs or None, extra_instruction=suggestions)
    res = _run("chapter", ctx)
    _persist_node(ws, project_id, f"chapter:{vol}:{ch}", res)
    bp.save(ws, project_id)
    sync_bible(ws, project_id, bp)
    mark_pending(ws, project_id, bp, [module], log_fn=log, vol=vol, ch=ch)
    new_text = gist_path.read_text(encoding="utf-8") if gist_path.exists() else ""
    import difflib

    return [ln for ln in difflib.unified_diff(
        old_text.splitlines(), new_text.splitlines(),
        fromfile="旧细纲", tofile="新细纲", lineterm="")][:80]


def diff_rows(old: Any, new: Any, path: str) -> list[str]:
    """大纲行旧→新确定性 diff（review.diff_section 的薄包装）。"""
    from .review import diff_section

    return diff_section(old, new, path)


@dataclass
class RollResult:
    ok: bool
    project_id: str
    vol: int
    calls_used: int = 0
    chapters_written: int = 0
    arcs_written: int = 0
    nodes_failed: int = 0   # 重试后仍失败的节点（改动已回滚）
    budget_exhausted: bool = False
    budget_limit: int = 40
    gate_halted: bool = False
    pending_review: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _roll_context(ws: Workspace, project_id: str, bp: Blueprint, vol: int, K: int) -> str:
    """§7.7 四块上下文注入（滚动优于一次生成的理由：贴合已发生的事实）。"""
    blocks: list[str] = []
    # 1. 前卷主线 + threads_to_payoff 兑现情况
    prev_plan = next((v for v in bp.section("volumes") if v.get("vol") == vol - 1), None) or {}
    threads = {t.get("id"): t for t in bp.section("threads")}
    payoff = [{"id": tid, "desc": (threads.get(tid) or {}).get("desc"),
               "status": (threads.get(tid) or {}).get("status")}
              for tid in (prev_plan.get("threads_to_payoff") or [])]
    blocks.append("【前卷主线】" + json.dumps(
        {k: prev_plan.get(k) for k in ("title", "summary", "key_beats") if prev_plan.get(k)},
        ensure_ascii=False))
    blocks.append("【前卷伏笔兑现情况】" + json.dumps(payoff, ensure_ascii=False))
    # 2. 前卷末 3 章实际发生的事件（memory/plot_events.json，实然而非规划态）
    events = [e for e in _load_json_list(ws, project_id, "memory/plot_events.json")
              if isinstance(e, dict) and int((e.get("at") or {}).get("vol") or 0) == vol - 1]
    if events:
        chs = sorted({int((e.get("at") or {}).get("ch") or 0) for e in events})
        last3 = set(chs[-3:]) if chs else set()
        recent = [str(e.get("summary")) for e in events
                  if int((e.get("at") or {}).get("ch") or 0) in last3][-12:]
        blocks.append("【前卷末 3 章实际发生】" + json.dumps(recent, ensure_ascii=False))
    else:
        blocks.append("【前卷末 3 章实际发生】（无记忆事件——前卷正文可能未经编纂员回写）")
    # 3. worldstate 现状（time.now / 人物状态 / 未回收 pending）
    try:
        wdata = json.loads(ws._abs(f"{project_id}/bible/worldstate.json").read_text(encoding="utf-8"))  # noqa: SLF001
    except (OSError, ValueError):
        wdata = {}
    now = int((wdata.get("time") or {}).get("now") or 0)
    chars_state = {cid: {k: c.get(k) for k in ("name", "realm", "location", "injuries", "dead") if c.get(k) is not None}
                   for cid, c in (wdata.get("characters") or {}).items()}
    open_pending = [{"id": p.get("id"), "what": p.get("what"), "due": p.get("due")}
                    for p in (wdata.get("pending") or [])
                    if isinstance(p, dict) and p.get("status") == "scheduled"]
    blocks.append("【世界现状】" + json.dumps(
        {"day": now, "characters": chars_state, "未回收定时事件": open_pending},
        ensure_ascii=False))
    # 4. 收尾清单（本卷必须回收的伏笔）
    try:
        from ..core.phase import payoff_checklist

        checklist = payoff_checklist(ws, project_id, vol, chapter_range_of(vol, K)[1])
    except Exception:  # noqa: BLE001 - 清单失败不阻断 roll
        checklist = {}
    blocks.append("【收尾清单】" + json.dumps(checklist, ensure_ascii=False, default=str))
    return "\n".join(blocks)


def roll(ws: Workspace, project_id: str, *, provider, vol: int,
         max_calls: int = 40, max_depth: int = 4, max_width: int = 4,
         gate: bool = True, resume: bool = False, coherence_review: bool = False,
         log_fn: Callable[[str], None] | None = None) -> RollResult:
    """滚动生成第 N 卷细纲（docs/10 §7.7）：注入四块上下文 → volume → arc? → chapter ×K → beat?。

    前置：vol ≥ 2 且第 vol−1 卷已有正文（chapters/<vol-1>-*.md）。
    末尾幂等追加新卷 after_days → worldstate.pending（due = 当前 now + 累计，
    id 沿用 pd:ke-<vol>-<ch>，已存在即跳过）。

    G1 修复（2026-09-05）：
    - `gate=True`（默认）：入口检查 ADR-024 待审模块，有待审即拦截；volume 产出
      按审核开关落 pending——原先 roll 完全绕过闸门；
    - `resume=True`：跳过已有细纲的章——原先重跑无条件覆盖全部旧细纲；
    - 调用数入 ForgeState 账（原先局部计数丢弃，跨阶段总量失真）；
    - `coherence_review=True`：逐章连读审查（与 build 同参同钩子）。
    """
    if vol <= 1:
        raise ValueError("roll 从第 2 卷起（卷 1 细纲由 forge build 生成）")
    bp = Blueprint.load(ws, project_id)
    scale = bp.get("meta.scale") or {}
    K = int(scale.get("chapters_per_volume", 20))
    chapters_dir = ws._abs(f"{project_id}/chapters")  # noqa: SLF001
    prev_written = list(chapters_dir.glob(f"{vol - 1}-*.md")) if chapters_dir.exists() else []
    if not prev_written:
        raise ValueError(f"第 {vol - 1} 卷无正文——roll 需前卷已有稿子（docs/10 §7.1 卷闸门）")
    # ---- 审核闸门（ADR-024，G1）：有待审模块 → 不消耗任何调用，直接交还用户 ----
    pending_now = sorted(pending_modules(ws, project_id)) if gate else []
    if pending_now:
        for m in pending_now:
            log_line = f"[gate] {REVIEW_MODULES[m]['label']}({m}) 待审核：forge review {m} → approve / revise"
            (log_fn or emit)(log_line)
        return RollResult(ok=True, project_id=project_id, vol=vol,
                          gate_halted=True, pending_review=pending_now,
                          warnings=["审核闸门拦截：先处置 pending 模块再 roll"])
    pack = _pack_for_bp(bp)
    state = ForgeState.load(ws, project_id)
    append_transcript(ws, project_id, "roll.start", rev=bp.data["rev"], vol=vol, max_calls=max_calls)
    log = log_fn or emit
    start = time.time()

    # roll 前置文件快照（F5：rollback / --diff 基线；与 build 同一机制）
    from .snapshot import take_snapshot

    snap = take_snapshot(ws, project_id, label=f"roll-v{vol}")
    log(f"[snapshot] {snap.name}")

    warnings: list[str] = []
    calls_used = 0
    chapters_written = 0
    arcs_written = 0
    nodes_ok = 0
    nodes_failed = 0
    streak = _FailStreak()
    budget_exhausted = False
    interrupted = False
    gate_halted = False

    def _budget_check() -> bool:
        nonlocal budget_exhausted
        if calls_used >= max_calls:
            budget_exhausted = True
            return False
        return True

    def _count_hook() -> None:
        """G2：审查调用计入调用预算（与 build 同语义）。"""
        nonlocal calls_used
        calls_used += 1

    def _call(ctx: NodeContext, kind: str, depth: int, vol_n: int = 0, ch: int = 0,
              child: dict | None = None):
        nonlocal calls_used, budget_exhausted, nodes_ok, nodes_failed
        label = _node_label(kind, vol_n, ch, child)
        if depth > max_depth:
            warnings.append(f"{label}: 超过 max_depth={max_depth}，按 done 处理")
            return None
        if not _budget_check():
            warnings.append(f"{label}: 预算耗尽（{calls_used}/{max_calls}），未生成")
            return None
        started = calls_used + 1
        log(f"[{started}/{max_calls} 调用] {label} … 生成中")  # UX-2：调用前可见
        counts_before = _bp_counts(bp)
        try:
            with heartbeat(label):
                res = run_node(ctx, kind)
            calls_used = started
            nodes_ok += 1
            streak.reset()
            ctx.retry_hint = ""
            log(f"[{calls_used}/{max_calls} 调用] {label} … ok ({time.time() - start:.1f}s)"
                + _delta_note(counts_before, _bp_counts(bp)))
            warnings.extend(f"{label}: {w}" for w in res.warnings)
            return res
        except ValueError as e:
            ctx.retry_hint = _retry_hint(label, e)   # 同 build：重试带上真实诊断
            if not _budget_check():
                nodes_failed += 1
                warnings.append(f"{label}: 重试时预算耗尽，未生成（沿用上一次可用产物）")
                _maybe_abort(streak, label)
                return None
            ctx.extra_warnings.append(f"{label}: 首次失败（{_one_line(e)}），重试中")
            try:
                res2 = run_node(ctx, kind)
                calls_used = started + 1
                nodes_ok += 1
                streak.reset()
                ctx.retry_hint = ""
                log(f"[{calls_used}/{max_calls} 调用] {label} … ok (retry)")
                warnings.extend(f"{label}: {w}" for w in res2.warnings)
                return res2
            except (ValueError, ModerationBlockedError) as e2:  # noqa: BLE001
                calls_used = started + 1
                nodes_failed += 1
                warnings.append(_fail_note(label, e2))
                _maybe_abort(streak, label)
                return None
        except ModerationBlockedError as e:
            # G1：roll 原先缺该分支——审核拦截被当普通异常吞掉，无痕迹
            calls_used = started
            nodes_failed += 1
            warnings.append(f"{label}: 审核拦截（{_one_line(e)}），该节点标记 blocked、人工补")
            _maybe_abort(streak, label)
            return None
        except Exception as e:  # noqa: BLE001
            calls_used = started
            nodes_failed += 1
            warnings.append(_fail_note(label, e))
            _maybe_abort(streak, label)
            return None

    try:
        # ---- volume（注入 §7.7 四块）----
        ctx = NodeContext(ws=ws, project_id=project_id, bp=bp, provider=provider, pack=pack,
                          vol=vol, extra={"roll_context": _roll_context(ws, project_id, bp, vol, K)})
        res = _call(ctx, "volume", 1, vol_n=vol)
        _persist_node(ws, project_id, f"volume:{vol}", res)
        bp.save(ws, project_id)
        # G1：审核闸门——volume 产出按开关落 pending（与 build 同语义，ADR-024）
        if gate and res is not None and res.ok:
            cfg = load_review(ws, project_id)
            gated_v = [m for m, s in REVIEW_MODULES.items()
                       if s["kind"] == "volume" and cfg["switches"].get(m, True)]
            if gated_v:
                mark_pending(ws, project_id, bp, gated_v, log_fn=log, vol=vol)
                raise _GateHalt(gated_v)
        # ---- arc? ----
        vol_arcs: list[dict] = []
        if res is not None and res.decide == "expand" and res.children:
            ck = CHILD_KIND["volume"]
            if len(res.children) > max_width:
                warnings.append(f"volume {vol}: children {len(res.children)} 个超出 max_width={max_width}，arc 截断")
            for i, raw in enumerate(res.children[:max_width], 1):
                item = raw if isinstance(raw, dict) else {"brief": str(raw)}
                item.setdefault("id", f"{ck}{i}")
                ctx_a = NodeContext(ws=ws, project_id=project_id, bp=bp, provider=provider,
                                    pack=pack, vol=vol, child=item)
                res_a = _call(ctx_a, "arc", 2, vol_n=vol, child=item)
                _persist_node(ws, project_id, f"arc:{vol}:{item['id']}", res_a)
                if res_a is not None and res_a.ok:
                    arcs_written += 1
                if budget_exhausted:
                    break
            bp.save(ws, project_id)
        vol_arcs = [x for x in _load_json_list(ws, project_id, "outline/arcs.json")
                    if int(x.get("vol") or 0) == vol]
        # ---- chapter ×K（prev 链从第 vol−1 卷末章接起）----
        from ..core.bible import parse_gist

        for ch in range(1, K + 1):
            if budget_exhausted:
                warnings.append(f"chapter {vol}-{ch}: 预算耗尽，未生成（剩余 {K - ch + 1} 章）")
                break
            gist_path = ws.outline_chapter_path(project_id, vol, ch)
            if resume and gist_path.exists():
                log(f"[skip] chapter {vol}-{ch}（outline 已有细纲）")
                continue
            prev = None
            if ch > 1:
                prev = parse_gist(ws, project_id, vol, ch - 1)
            else:
                prev = parse_gist(ws, project_id, vol - 1, K)  # 前卷末章衔接
            ctx_ch = NodeContext(ws=ws, project_id=project_id, bp=bp, provider=provider,
                                 pack=pack, vol=vol, ch=ch, prev_gist=prev,
                                 arcs=vol_arcs or None)
            # G4：连读审查禁令注入本章细纲 prompt（与 build 同语义）
            if coherence_review and ch >= 2:
                try:
                    from .coherence import load_coherence_bans

                    ctx_ch.extra["coherence_bans"] = load_coherence_bans(
                        ws, project_id, vol, ch)
                except Exception:  # noqa: BLE001
                    ctx_ch.extra["coherence_bans"] = []
            res_ch = _call(ctx_ch, "chapter", 2, vol_n=vol, ch=ch)
            if res_ch is not None and res_ch.ok:
                chapters_written += 1
                # G1：审核闸门——chapter 产出按开关落 pending（与 build 同语义）
                if gate:
                    cfg_c = load_review(ws, project_id)
                    gated_c = [m for m, s in REVIEW_MODULES.items()
                               if s["kind"] == "chapter" and cfg_c["switches"].get(m, True)]
                    if gated_c:
                        mark_pending(ws, project_id, bp, gated_c, log_fn=log,
                                     vol=vol, ch=ch)
                        raise _GateHalt(gated_c)
                # 细纲连读审查（opt-in，≥2 章触发；调用计入预算）
                if coherence_review and ch >= 2:
                    try:
                        from .coherence import run_coherence_review

                        fnd = run_coherence_review(ws, project_id, bp, provider, vol, ch,
                                                   count_hook=_count_hook)
                        if fnd:
                            nm = len(fnd.get("motif_repeats") or [])
                            nc = len(fnd.get("causal_issues") or [])
                            log(f"[coherence] {vol}-{ch} 连读审查：母题重复{nm} 因果{nc}")
                    except Exception as e:  # noqa: BLE001
                        warnings.append(f"coherence {vol}-{ch}: {type(e).__name__}: {e}"[:120])
            bp.save(ws, project_id)
            # beat?
            if res_ch is not None and res_ch.ok \
                    and res_ch.decide == "expand" and res_ch.children:
                briefs = "; ".join(
                    str((c if isinstance(c, dict) else {"brief": str(c)}).get("brief") or "")
                    for c in res_ch.children[:max_width]).strip("; ")
                ctx_b = NodeContext(ws=ws, project_id=project_id, bp=bp, provider=provider,
                                    pack=pack, vol=vol, ch=ch,
                                    child={"id": f"beat{ch}", "brief": briefs})
                res_b = _call(ctx_b, "beat", 3, vol_n=vol, ch=ch, child=ctx_b.child)
                _persist_node(ws, project_id, f"beat:{vol}:{ch}", res_b)
                bp.save(ws, project_id)
    except _GateHalt as g:
        gate_halted = True
        warnings.append(f"审核闸门暂停（待处置：{g.modules}）——approve 后可 roll/resume 续跑")
    except _FailAbort as a:
        interrupted = True
        warnings.append(str(a))
    except KeyboardInterrupt:
        interrupted = True
        warnings.append("SIGINT：当前节点后落盘退出")
    finally:
        # ---- 幂等追加新卷 after_days → worldstate.pending（不覆盖 time/characters）----
        _append_roll_pending(ws, project_id, bp, vol)
        # G1：调用数入 ForgeState 账（原先局部计数丢弃，跨阶段总量失真）
        state.calls_used = int(state.calls_used or 0) + calls_used
        state.touch_stage(ws, project_id, state.stage or "built")
        append_transcript(ws, project_id, "roll.end", vol=vol, calls_used=calls_used,
                          chapters_written=chapters_written,
                          budget_exhausted=budget_exhausted, interrupted=interrupted,
                          gate_halted=gate_halted,
                          warnings=warnings[:10])
        # 结果快照（F5e diff 基线 / 默认 rollback 点）
        from .snapshot import take_snapshot

        take_snapshot(ws, project_id, label=f"roll-ok-v{vol}")

    _log_round_summary(log, kind=f"滚动第 {vol} 卷", calls_used=calls_used,
                       max_calls=max_calls, nodes_done=nodes_ok, nodes_failed=nodes_failed,
                       elapsed_s=time.time() - start, gate_halted=gate_halted,
                       budget_exhausted=budget_exhausted, interrupted=interrupted)
    return RollResult(
        ok=not interrupted and not budget_exhausted and (chapters_written > 0 or gate_halted),
        project_id=project_id, vol=vol, calls_used=calls_used,
        nodes_failed=nodes_failed,
        chapters_written=chapters_written, arcs_written=arcs_written,
        budget_exhausted=budget_exhausted, budget_limit=max_calls,
        gate_halted=gate_halted,
        pending_review=sorted(pending_modules(ws, project_id)) if gate_halted else [],
        warnings=warnings,
    )


def _append_roll_pending(ws: Workspace, project_id: str, bp: Blueprint, vol: int) -> None:
    """新卷细纲 after_days → worldstate.pending 追加（幂等；due = 当前 now + 累计）。"""
    p = ws.bible_path(project_id, "worldstate")
    try:
        wdata = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(wdata, dict):
        return
    now = int((wdata.get("time") or {}).get("now") or 0)
    existing_ids = {x.get("id") for x in (wdata.get("pending") or []) if isinstance(x, dict)}
    t = 0
    added = False
    for g in sorted(bp.section("chapters"), key=lambda x: (int(x.get("vol") or 1), int(x.get("ch") or 0))):
        if int(g.get("vol") or 0) != vol:
            continue
        n = int(g.get("after_days") or 0)
        t += n
        if n <= 0:
            continue
        ch = int(g.get("ch") or 0)
        pid_ = f"pd:ke-{vol}-{ch}"
        if pid_ in existing_ids:
            continue
        events = [str(e) for e in (g.get("key_events") or []) if str(e).strip()]
        wdata.setdefault("pending", []).append({
            "id": pid_, "who": "",
            "what": events[0] if events else str(g.get("title") or f"第 {ch} 章"),
            "due": now + t, "span": max(t, 1), "created_t": now,
            "status": "scheduled", "created_at": {"vol": vol, "ch": ch},
            "overdue": 0, "block_count": 0,
        })
        added = True
    if added:
        ws.write_json(p, wdata)


# ---- 未来窗口滚动细纲（承诺账本门控，docs/10 §7.7 补）----


@dataclass
class RollWindowResult:
    """一次未来窗口滚动的结果（关键在 `gate_halted`：触及承诺需人工闸门）。"""

    ok: bool
    project_id: str
    vol: int
    start_ch: int          # 本次窗口起点（未来第一未写章）
    width_applied: int = 0  # 实际重生成的章数
    calls_used: int = 0
    budget_limit: int = 8
    budget_exhausted: bool = False
    gate_halted: bool = False      # 触及承诺 → 已 mark_pending，改动未落盘
    pending_review: list[str] = field(default_factory=list)
    touched: list[str] = field(default_factory=list)   # 被触及的承诺条目 key
    warnings: list[str] = field(default_factory=list)
    diffs: list[str] = field(default_factory=list)     # 各章旧→新事件确定性 diff 行


def _window_instruction(vol: int, ch: int, old_gist: dict) -> str:
    """未来窗口滚动提醒：可变层修订 + 承诺边界禁令 + 旧 key_events 保留。"""
    old_ke = "；".join(str(e) for e in (old_gist.get("key_events") or [])) or "（无）"
    return (
        f"这是第 {vol} 卷「未来 2-3 章窗口」内的滚动修订（可变层，docs/10 承诺账本）。\n\n"
        f"故事已实际写到第 {vol} 卷第 {ch - 1} 章。本章尚未成文，允许在此刻度修订其"
        f"执行层内容，以便与本章【已落定实情】衔接、让细纲跟上故事实际走向。\n\n"
        f"【本章旧细纲的 key_events（以此为基础修订，勿整体推翻方向）】\n{old_ke}\n\n"
        f"要求：\n"
        f"1. 只调整执行层：title/key_events/turns/tension/hook/characters/after_days 等；"
        f"key_events 可重排、可改写实现手段。\n"
        f"2. 承诺边界（恒定层，严禁本节点改动）：任何伏笔的 status/target_vol、"
        f"卷主线的 threads_to_payoff、上述模块 summary、任一核心角色（主角/宿敌/男主/女主/"
        f"师尊等）的人设/弧线/结局/存活状态——它们由承诺账本锁定，只允许人工审核后修改。\n"
        f"3. 若你的修订需要触碰上述承诺项：在本轮 reason 里明确写「需人工审核」，"
        f"并保持承诺字段原样，交给审核闸门，不要在本节点直接改。\n"
        f"4. key_events 仍须与【已规划事件账本】规避重复（沿用章节细纲协议纪律）。"
    )


def _rerun_chapter(ws, project_id, bp, provider, *, vol: int, ch: int,
                   extra_instruction: str):
    """重生成单章细纲（删旧 nodes 产物 → 带 extra 重跑 chapter 节点，retry=1）。

    复用于 `revise_module` 的 chapter 分支同机制。返回 NodeResult（已 apply 落盘
    bp.chapters + outline/chapters md；抛 ValueError 表示重试后仍解析失败）。
    """
    from ..core.bible import parse_gist

    nodes_d = ws._abs(f"{project_id}/workspace/forge/nodes")  # noqa: SLF001
    (nodes_d / f"chapter-{vol}-{ch}.json").unlink(missing_ok=True)
    prev = parse_gist(ws, project_id, vol, ch - 1) if ch > 1 else None
    arcs = [x for x in _load_json_list(ws, project_id, "outline/arcs.json")
            if int(x.get("vol") or 0) == vol]
    ctx = NodeContext(ws=ws, project_id=project_id, bp=bp, provider=provider,
                      pack=_pack_for_bp(bp), vol=vol, ch=ch, prev_gist=prev,
                      arcs=arcs or None, extra_instruction=extra_instruction)
    try:
        return run_node(ctx, "chapter")
    except ValueError as e:
        ctx.extra_instruction = f"{ctx.extra_instruction}\n（上次输出解析失败：{e}，请修正格式）"
        return run_node(ctx, "chapter")


def _volume_chapter_range(bp: Blueprint, vol: int) -> int:
    scale = bp.get("meta.scale") or {}
    return int(scale.get("chapters_per_volume", 20))


def roll_window(ws: Workspace, project_id: str, *, provider: Any, vol: int,
                **kw: Any) -> "RollWindowResult":
    """未来窗口滚动细纲（薄包装）：给原始调用日志挂 forge:roll-window 上下文。"""
    from ..core.calllog import call_context

    with call_context("forge:roll-window"):
        return _roll_window_impl(ws, project_id, provider=provider, vol=vol, **kw)


def _roll_window_impl(ws: Workspace, project_id: str, *, provider, vol: int,
                      from_ch: int | None = None, width: int = 3,
                gate: bool = True, max_calls: int | None = None,
                log_fn: Callable[[str], None] | None = None) -> RollWindowResult:
    """未来 2-3 章窗口滚动细纲（docs/10 §7.7 可变层）。

    **语义**：故事写到卷内某处后，"未来窗口"（尚未成文的下一个 `width` 章）允许在此
    刻度上修订其 key_events 等执行层，好贴合已发生的事实。整个修订**先过承诺账本门**：
    - 未触及任何承诺 → 自动落盘（细纲 md + 蓝图 chapters）；
    - 触及承诺（伏笔/卷主线/核心人设）→ **回滚**窗口改动 + `mark_pending` 人工闸门
      （ADR-024），绝不静默改承诺。

    这是"卷末纠偏/章节间纠偏"的入口：在任何未写章前调用 `roll_window(vol, from_ch=下一章)`；
    窗口越过卷尾时钳制在卷内；若整卷已写完（无未来窗口）则提示用 `forge roll <vol+1>` 衔接。

    - `from_ch` 缺省 = 卷内第一未写章（自动定位）。
    - `gate=False` 关承诺门（脚本/测试直跑用，CLI 默认开）。
    - 返回 `RollWindowResult`；触及承诺时 `gate_halted=True` + `touched` 列出承诺 key。
    """
    log = log_fn or emit
    bp = Blueprint.load(ws, project_id)
    K = _volume_chapter_range(bp, vol)
    budget = int(max_calls or 0) or (width * 2 + 2)
    written = {c for c in range(1, K + 1)
               if ws.chapter_path(project_id, vol, c).exists()}
    start = max(1, from_ch if from_ch is not None
                else ((max(written) + 1) if written else 1))
    if start > K:
        return RollWindowResult(
            ok=True, project_id=project_id, vol=vol, start_ch=K,
            warnings=[f"第 {vol} 卷已无未来窗口（写到卷末）。下一卷细纲用 "
                      f"`forge roll {vol + 1}` 衔接；本卷收尾如需纠偏，可检查 "
                      f"`forge covenant` 承诺账本"]) 
    window = [c for c in range(start, K + 1) if c not in written][:max(1, width)]
    if not window:
        return RollWindowResult(
            ok=True, project_id=project_id, vol=vol, start_ch=start,
            warnings=[f"第 {vol} 卷未来窗口为空：从起始章起均已有正文，无可滚动的未写章。"
                      f"下一卷细纲用 `forge roll {vol + 1}` 衔接；卷内结构纠偏走 "
                      f"`forge covenant` 承诺账本"])

    # ---- 快照 + 前置账本（F5：rollback 基线；covenant 触发判定用）----
    from .snapshot import take_snapshot

    snap = take_snapshot(ws, project_id, label=f"roll-window-v{vol}-{start}")
    log(f"[snapshot] {snap.name}")
    bp_pre = copy.deepcopy(bp)
    covenant = build_covenant(bp)
    pre_mds = {c: (ws._abs(f"{project_id}/outline/chapters/{vol}-{c}.md")  # noqa: SLF001
                   .read_text(encoding="utf-8") if ws._abs(f"{project_id}/outline/chapters/{vol}-{c}.md")  # noqa: SLF001
                   .exists() else None) for c in window}
    pre_nodes = {c: ws._abs(f"{project_id}/workspace/forge/nodes/"  # noqa: SLF001
                            f"chapter-{vol}-{c}.json").exists() for c in window}

    warnings: list[str] = []
    diffs: list[str] = []
    calls_used = 0
    budget_exhausted = False

    def _budget_check() -> bool:
        nonlocal budget_exhausted
        if calls_used >= budget:
            budget_exhausted = True
            return False
        return True

    for ch in window:
        if budget_exhausted:
            warnings.append(f"chapter {vol}-{ch}: 窗口预算耗尽，未滚动")
            break
        from ..core.bible import parse_gist

        old = parse_gist(ws, project_id, vol, ch) or {"key_events": []}
        prev_ke = list(old.get("key_events") or []) if isinstance(old, dict) else []
        started = calls_used + 1
        log(f"[{started}/{budget} 调用] chapter {vol}-{ch} 窗口重生成 … 生成中")  # UX-2
        try:
            with heartbeat(f"chapter {vol}-{ch} 窗口重生成"):
                res = _rerun_chapter(ws, project_id, bp, provider,
                                     vol=vol, ch=ch,
                                     extra_instruction=_window_instruction(vol, ch, old))
            calls_used = started
        except (ValueError, ModerationBlockedError) as e:  # noqa: BLE001
            calls_used = started
            warnings.append(f"chapter {vol}-{ch}: 滚动重生成失败（保留旧细纲）: {e}")
            continue
        except Exception as e:  # noqa: BLE001 - 节点异常不拖垮整个窗口
            calls_used = started
            warnings.append(f"chapter {vol}-{ch}: 滚动异常（保留旧细纲）: {e}")
            continue
        if res is None or not res.ok:
            calls_used = started
            warnings.append(f"chapter {vol}-{ch}: 节点未产出，保留旧细纲")
            continue
        new = parse_gist(ws, project_id, vol, ch) or {}
        new_ke = list(new.get("key_events") or []) if isinstance(new, dict) else []
        if str(prev_ke) != str(new_ke):
            diffs.append(f"{vol}-{ch} key_events:\n  - {prev_ke}\n  + {new_ke}")
        if res.warnings:
            warnings.extend(f"chapter {vol}-{ch}: {w}" for w in res.warnings)
        log(f"[{calls_used}/{budget}] window {vol}-{ch} 滚动 ok")
        bp.save(ws, project_id)

    if budget_exhausted:
        warnings.append(f"窗口预算耗尽（{calls_used}/{budget}），剩余窗口未滚动")
    sync_bible(ws, project_id, bp)

    # ---- 承诺门：改动先过 touched_entries ----
    touched = touched_entries(bp_pre, bp, covenant)
    if touched:
        # 回滚窗口改动（细纲 md + 蓝图 chapters + nodes 产物），保住"未落盘的承诺"
        for c in window:
            md = ws._abs(f"{project_id}/outline/chapters/{vol}-{c}.md")  # noqa: SLF001
            pre = pre_mds.get(c)
            if pre is None and md.exists():
                md.unlink()
            elif pre is not None:
                md.parent.mkdir(parents=True, exist_ok=True)
                md.write_text(pre, encoding="utf-8")
            node_f = ws._abs(f"{project_id}/workspace/forge/nodes/"  # noqa: SLF001
                             f"chapter-{vol}-{c}.json")
            if not pre_nodes.get(c) and node_f.exists():
                node_f.unlink()
        bp.data["chapters"] = copy.deepcopy(bp_pre.data["chapters"])
        bp.save(ws, project_id)
        sync_bible(ws, project_id, bp)
        modules = affected_modules(touched)
        msg = "承诺账本触及，窗口改动**未落盘**转入人工审核"
        if gate:
            try:
                mark_pending(ws, project_id, bp, modules, log_fn=log)
                msg += f"（已 mark_pending: {modules}）"
            except Exception as e:  # noqa: BLE001 - 审批落盘失败不强阻
                warnings.append(f"mark_pending 失败: {e}")
        warnings.append(msg + f" 触及承诺: {[str(e.key) for e in touched]}")
        log(f"[gate] {msg}：{', '.join(str(e.key) for e in touched)}")
        state = ForgeState.load(ws, project_id)
        state.calls_used = int(state.calls_used or 0) + calls_used
        state.save(ws, project_id)
        append_transcript(ws, project_id, "roll_window.end", vol=vol, start_ch=start,
                          calls_used=calls_used, gate_halted=True,
                          touched=[str(e.key) for e in touched], warnings=warnings[:8])
        return RollWindowResult(
            ok=False, project_id=project_id, vol=vol, start_ch=start,
            width_applied=0, calls_used=calls_used, budget_limit=budget,
            gate_halted=True, pending_review=modules,
            touched=[str(e.key) for e in touched], warnings=warnings, diffs=diffs)

    # ---- 未触及承诺 → 自动落盘（窗口章节已随重生成落盘）----
    state = ForgeState.load(ws, project_id)
    state.calls_used = int(state.calls_used or 0) + calls_used
    state.save(ws, project_id)
    append_transcript(ws, project_id, "roll_window.end", vol=vol, start_ch=start,
                      width_applied=len(window), calls_used=calls_used,
                      gate_halted=False, warnings=warnings[:8])
    return RollWindowResult(
        ok=True, project_id=project_id, vol=vol, start_ch=start,
        width_applied=len(window), calls_used=calls_used, budget_limit=budget,
        budget_exhausted=budget_exhausted, warnings=warnings, diffs=diffs)


def _upsert_volume_from_row(bp: Blueprint, row: dict) -> None:
    """resume 时把已落盘卷条目同步回蓝图（幂等，不覆盖 user 保护字段）。"""
    vols = bp.section("volumes")
    vol = int(row.get("vol", 0))
    if vol <= 0:
        return
    for i, x in enumerate(vols):
        if x.get("vol") == vol:
            merged = dict(x)
            for k, v in row.items():
                if k == "vol":
                    continue
                if bp.is_protected(f"volumes[{vol}].{k}"):
                    continue
                merged[k] = v
            vols[i] = merged
            return
    vols.append(dict(row))


# ---- F5e：`forge build --diff` 影响分析（docs/10 §9.6）----

@dataclass
class DiffPlan:
    """当前蓝图 vs 最近快照的差异影响：变更实体 → 受影响节点/章。

    - rebuild_all：worldview/style/volumes 结构级变更 → 全量重建（不删 nodes，
      交给 build 自身幂等逻辑；此处只删章级产物加速）。
    - affected_chapters：按引用精确匹配的章（characters/threads 变更）。
    - affected_nodes：要删的 nodes/<id>.json（含净化名）。
    """

    changed: list[str] = field(default_factory=list)
    rebuild_all: bool = False
    affected_chapters: list[tuple[int, int]] = field(default_factory=list)
    affected_nodes: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        parts = []
        if self.rebuild_all:
            parts.append("全量重建")
        if self.affected_chapters:
            chs = ",".join(f"{v}-{c}" for v, c in sorted(self.affected_chapters))
            parts.append(f"重建 {len(self.affected_chapters)} 章: {chs}")
        if self.affected_nodes:
            parts.append(f"清节点 {len(self.affected_nodes)} 个")
        return "; ".join(parts) if parts else "无变更"

    def apply(self, ws: Workspace, project_id: str) -> int:
        """删除受影响产物（nodes/ 文件 + 细纲 md）。返回删除文件数。"""
        removed = 0
        nodes_dir = ws._abs(f"{project_id}/workspace/forge/nodes")  # noqa: SLF001
        for nid in self.affected_nodes:
            p = nodes_dir / (nid.replace(":", "-") + ".json")
            if p.exists():
                p.unlink()
                removed += 1
        for vol, ch in self.affected_chapters:
            gist = ws._abs(f"{project_id}/outline/chapters/{vol}-{ch}.md")  # noqa: SLF001
            if gist.exists():
                gist.unlink()
                removed += 1
            b = nodes_dir / f"beat-{vol}-{ch}.json"
            if b.exists():
                b.unlink()
                removed += 1
        return removed


def _section_by_id(bp: Blueprint, name: str) -> dict:
    return {str(x.get("id", i)): json.dumps(x, ensure_ascii=False, sort_keys=True)
            for i, x in enumerate(bp.section(name))}


def diff_affected(ws: Workspace, project_id: str) -> DiffPlan:
    """当前 blueprint vs 最近快照内 blueprint 的影响分析（F5e）。

    无快照 → 空 plan（首轮构建没有可比对的基线；build 正常全量跑）。
    worldview/style/volumes 结构变更 → rebuild_all；characters/threads 变更 →
    精确匹配引用它们的章；settings 变更只清 setting_entry 节点。
    """
    from .snapshot import snapshot_blueprint

    plan = DiffPlan()
    bp = Blueprint.load(ws, project_id)
    snap = snapshot_blueprint(ws, project_id)
    if snap is None:
        plan.changed.append("无快照基线（首轮构建，--diff 退化为全量 build）")
        return plan
    if bp.data.get("rev") == snap.get("rev"):
        plan.changed.append("rev 未变，无差异")
        return plan

    cur = bp.data
    # 1) 结构级字段：worldview / style / volumes / arcs
    for key, label in (("worldview", "worldview"), ("style", "style"),
                       ("volumes", "volumes"), ("arcs", "arcs")):
        if json.dumps(cur.get(key), ensure_ascii=False, sort_keys=True) \
                != json.dumps(snap.get(key), ensure_ascii=False, sort_keys=True):
            plan.rebuild_all = True
            plan.changed.append(f"{label} 变更")
    if plan.rebuild_all:
        return plan  # 结构级变更覆盖一切，无需精确定位

    # 2) 实体级：characters / threads / settings（按 id 序列化 diff）
    affected: dict[str, set[tuple[int, int]]] = {}
    for name, label in (("characters", "人物"), ("threads", "伏笔"), ("settings", "设定")):
        cur_map = _section_by_id(bp, name)
        snap_map = {str(x.get("id", i)): json.dumps(x, ensure_ascii=False, sort_keys=True)
                    for i, x in enumerate(snap.get(name) or [])}
        added = {k for k in cur_map if k not in snap_map}
        dropped = {k for k in snap_map if k not in cur_map}
        changed = {k for k in cur_map if k in snap_map and cur_map[k] != snap_map[k]}
        if added or dropped or changed:
            plan.changed.append(f"{label} 变更 {len(added)}+{len(dropped)}+{len(changed)}"
                                f"（新/删/改）")
            affected[name] = added | dropped | changed

    if not affected:
        plan.changed.append("实体无差异（仅 rev 或 meta 变更）")
        return plan

    # 3) 章引用匹配
    vols = _load_volumes(ws, project_id)
    gists = _load_gists(ws, project_id, vols)
    char_ids = affected.get("characters")
    thread_ids = affected.get("threads")
    for (vol, ch), g in sorted(gists.items()):
        hit = False
        if char_ids:
            cast = {str(c) for c in (g.get("characters") or [])}
            hit = bool(cast & char_ids)
        if not hit and thread_ids:
            tids = {str(t) for t in (g.get("threads_involved") or [])}
            hit = bool(tids & thread_ids)
        if hit:
            plan.affected_chapters.append((vol, ch))
            plan.affected_nodes.append(f"chapter:{vol}:{ch}")
            plan.affected_nodes.append(f"beat:{vol}:{ch}")
    # settings 变更：清 setting_entry 节点（细纲无引用字段；知识库按需检索）
    if affected.get("settings"):
        plan.affected_nodes.append("setting_entry")
    return plan


def _load_volumes(ws: Workspace, project_id: str) -> list[dict]:
    p = ws._abs(f"{project_id}/outline/volumes.json")  # noqa: SLF001
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [v for v in data if isinstance(v, dict)] if isinstance(data, list) else []


def _load_gists(ws: Workspace, project_id: str, vols: list[dict]) -> dict[tuple[int, int], dict]:
    from ..core.bible import parse_gist

    out: dict[tuple[int, int], dict] = {}
    for row in vols:
        rng = row.get("chapter_range") or [0, -1]
        try:
            s, e = int(rng[0]), int(rng[1])
        except (TypeError, ValueError, IndexError):
            continue
        vol = int(row.get("vol", 0))
        for ch in range(s, e + 1):
            g = parse_gist(ws, project_id, vol, ch)
            if g is not None:
                out[(vol, ch)] = g
    return out
