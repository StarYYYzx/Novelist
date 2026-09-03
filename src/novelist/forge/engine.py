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

硬边界（docs/10 §7.3）：max_calls 分阶段配额（build 卷 1 默认 60 / roll 每卷 40）；
max_depth=4（book=0 … beat=4，超深强制 done）；max_width=4（children 截断）；
每节点 max_retries=1（解析失败重试一次，再失败回退父层产物）。

续跑：chapter/volume 沿用产物存在性判据；旁支/arc/beat 用 `nodes/<node_id>.json`
落盘判据（docs/10 §7.4 增量落盘）。SIGINT 在当前节点后落盘退出。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..core.llm import ModerationBlockedError
from ..storage.workspace import Workspace
from . import genres as _genres
from .nodes import (CHILD_KIND, LEAF_KINDS, NodeContext, chapter_range_of, run_node,
                    sync_bible, synthesize_worldstate, _load_json_list)
from .review import (REVIEW_MODULES, load_review, mark_pending, pending_modules,
                     render_review_md, resolve_pending, stage_pending_for_revise)
from .state import Blueprint, ForgeState, append_transcript


@dataclass
class BuildResult:
    ok: bool
    project_id: str
    calls_used: int = 0
    nodes_done: int = 0
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


def _node_done(ws: Workspace, project_id: str, node_id: str) -> bool:
    fname = node_id.replace(":", "-") + ".json"
    return ws._abs(f"{project_id}/workspace/forge/nodes/{fname}").exists()  # noqa: SLF001


def build(ws: Workspace, project_id: str, *, provider,
          max_calls: int = 60, max_depth: int = 4, max_width: int = 4,
          spec: Any = None, pack: dict | None = None,
          resume: bool = False, deepen: bool = True,
          gate: bool = True,
          log_fn: Callable[[str], None] | None = None) -> BuildResult:
    """全权构建：book → 旁支 DFS（deepen）→ volume(全卷) → arc?/chapter(仅 vol=1)/beat?，
    末尾 worldstate 确定性合成。

    `resume=True`：跳过已落盘节点（幂等续跑）。`deepen=False` 退化为 F1 最小树。
    `gate=False`：关闭审核闸门（ADR-024；单测/脚本直跑用，CLI 默认开）。
    `log_fn` 缺省打印进度行 `[calls/max] <node> … ok (calls=N)`。
    """
    bp = Blueprint.load(ws, project_id)
    meta = bp.get("meta") or {}
    scale = meta.get("scale")
    if not scale:
        raise ValueError(f"{project_id}: 蓝图缺 meta.scale——先跑 `forge seed`")
    N = int(scale.get("volumes", 3))
    K = int(scale.get("chapters_per_volume", 20))
    pack = pack or _pack_for_bp(bp)
    state = ForgeState.load(ws, project_id)
    state.stage = "build"
    calls_used = int(state.calls_used or 0)
    log = log_fn or (lambda line: print(line, flush=True))

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

    # 构建前置文件快照（docs/10 §7.6 F5）：rollback / --diff 的基线
    from .snapshot import take_snapshot

    snap = take_snapshot(ws, project_id, label="build")
    log(f"[snapshot] {snap.name}")

    warnings: list[str] = []
    nodes_done = 0
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
        nonlocal calls_used, nodes_done
        label = _node_label(kind, vol, ch, child)
        if depth > max_depth:
            warnings.append(f"{label}: 超过 max_depth={max_depth}，按 done 处理")
            return None
        if not _budget_check():
            warnings.append(f"{label}: 预算耗尽（{calls_used}/{max_calls}），未生成")
            return None
        started = calls_used + 1
        try:
            res = run_node(ctx, kind)
            calls_used = started
            nodes_done += 1
            log(f"[{calls_used}/{max_calls}] {label} … ok (calls={calls_used}, "
                f"{time.time() - start:.1f}s)")
            for w in res.warnings:
                warnings.append(f"{label}: {w}")
            append_transcript(ws, project_id, "build_node", kind=kind, node=label,
                              decide=res.decide, reason=(res.reason or "")[:120],
                              tokens_in=res.tokens_in, tokens_out=res.tokens_out)
            return res
        except ValueError as e:  # 解析失败 → retry 1（docs/10 §12）
            if not _budget_check():
                warnings.append(f"{label}: 重试时预算耗尽，回退父层产物")
                return None
            ctx.extra_warnings.append(f"{label}: 首次解析失败（{e}），重试中")
            try:
                res2 = run_node(ctx, kind)
                calls_used = started + 1
                nodes_done += 1
                log(f"[{calls_used}/{max_calls}] {label} … ok (retry, calls={calls_used}, "
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
                warnings.append(f"{label}: 重试仍失败，回退父层产物: {e2}")
                return None
        except ModerationBlockedError as e:
            calls_used = started
            warnings.append(f"{label}: 审核拦截（{e}），该节点标记 blocked、人工补")
            return None
        except Exception as e:  # noqa: BLE001 - 节点级失败不拖垮整棵树
            calls_used = started
            warnings.append(f"{label}: 节点异常，回退父层产物: {e}")
            return None

    try:
        # ---- L0 book ----
        book_done = bool(bp.section("volumes")) if resume else False
        if not book_done:
            ctx = NodeContext(ws=ws, project_id=project_id, bp=bp, provider=provider,
                              pack=pack, spec=spec)
            res = _call(ctx, "book", 0)
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
                if len(res.children) > max_width:
                    warnings.append(f"{nid}: children {len(res.children)} 个超出 max_width={max_width}，截断")
                for i, raw in enumerate(res.children[:max_width], 1):
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
                res_ch = _call(ctx_ch, "chapter", 2, vol=1, ch=ch)
                if res_ch is not None and res_ch.ok:
                    chapters_written += 1
                    gated_c = _gated("chapter")
                    if gated_c:
                        mark_pending(ws, project_id, bp, gated_c, log_fn=log,
                                     vol=1, ch=ch)
                        raise _GateHalt(gated_c)
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
    except KeyboardInterrupt:
        interrupted = True
        warnings.append("SIGINT：当前节点后落盘退出（被中断节点未落盘、calls_used 不回退）")
    finally:
        # ---- 末尾：worldstate 确定性合成（零 LLM，docs/10 §7.5）----
        wstate = synthesize_worldstate(bp)
        ws.write_json(ws.bible_path(project_id, "worldstate"), wstate)
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
        # 结果快照（F5e diff 基线 / 默认 rollback 点）：build 完成后的产物状态
        from .snapshot import take_snapshot

        take_snapshot(ws, project_id, label="build-ok")

    ok = (not interrupted and not budget_exhausted
          and (chapters_written > 0 or gate_halted))
    return BuildResult(
        ok=ok,
        project_id=project_id,
        calls_used=calls_used,
        nodes_done=nodes_done,
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
    log = log_fn or (lambda line: print(line, flush=True))
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
    budget_exhausted: bool = False
    budget_limit: int = 40
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
         log_fn: Callable[[str], None] | None = None) -> RollResult:
    """滚动生成第 N 卷细纲（docs/10 §7.7）：注入四块上下文 → volume → arc? → chapter ×K → beat?。

    前置：vol ≥ 2 且第 vol−1 卷已有正文（chapters/<vol-1>-*.md）。
    末尾幂等追加新卷 after_days → worldstate.pending（due = 当前 now + 累计，
    id 沿用 pd:ke-<vol>-<ch>，已存在即跳过）。
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
    pack = _pack_for_bp(bp)
    state = ForgeState.load(ws, project_id)
    append_transcript(ws, project_id, "roll.start", rev=bp.data["rev"], vol=vol, max_calls=max_calls)
    log = log_fn or (lambda line: print(line, flush=True))
    start = time.time()

    # roll 前置文件快照（F5：rollback / --diff 基线；与 build 同一机制）
    from .snapshot import take_snapshot

    snap = take_snapshot(ws, project_id, label=f"roll-v{vol}")
    log(f"[snapshot] {snap.name}")

    warnings: list[str] = []
    calls_used = 0
    chapters_written = 0
    arcs_written = 0
    budget_exhausted = False
    interrupted = False

    def _budget_check() -> bool:
        nonlocal budget_exhausted
        if calls_used >= max_calls:
            budget_exhausted = True
            return False
        return True

    def _call(ctx: NodeContext, kind: str, depth: int, vol_n: int = 0, ch: int = 0,
              child: dict | None = None):
        nonlocal calls_used, budget_exhausted
        label = _node_label(kind, vol_n, ch, child)
        if depth > max_depth:
            warnings.append(f"{label}: 超过 max_depth={max_depth}，按 done 处理")
            return None
        if not _budget_check():
            warnings.append(f"{label}: 预算耗尽（{calls_used}/{max_calls}），未生成")
            return None
        started = calls_used + 1
        try:
            res = run_node(ctx, kind)
            calls_used = started
            log(f"[{calls_used}/{max_calls}] {label} … ok ({time.time() - start:.1f}s)")
            warnings.extend(f"{label}: {w}" for w in res.warnings)
            return res
        except ValueError as e:
            ctx.extra_warnings.append(f"{label}: 首次解析失败（{e}），重试中")
            if not _budget_check():
                warnings.append(f"{label}: 重试时预算耗尽，回退父层产物")
                return None
            try:
                res2 = run_node(ctx, kind)
                calls_used = started + 1
                log(f"[{calls_used}/{max_calls}] {label} … ok (retry)")
                warnings.extend(f"{label}: {w}" for w in res2.warnings)
                return res2
            except (ValueError, ModerationBlockedError) as e2:  # noqa: BLE001
                calls_used = started + 1
                warnings.append(f"{label}: 重试仍失败，回退父层产物: {e2}")
                return None
        except Exception as e:  # noqa: BLE001
            calls_used = started
            warnings.append(f"{label}: 节点异常，回退父层产物: {e}")
            return None

    try:
        # ---- volume（注入 §7.7 四块）----
        ctx = NodeContext(ws=ws, project_id=project_id, bp=bp, provider=provider, pack=pack,
                          vol=vol, extra={"roll_context": _roll_context(ws, project_id, bp, vol, K)})
        res = _call(ctx, "volume", 1, vol_n=vol)
        _persist_node(ws, project_id, f"volume:{vol}", res)
        bp.save(ws, project_id)
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
            prev = None
            if ch > 1:
                prev = parse_gist(ws, project_id, vol, ch - 1)
            else:
                prev = parse_gist(ws, project_id, vol - 1, K)  # 前卷末章衔接
            ctx_ch = NodeContext(ws=ws, project_id=project_id, bp=bp, provider=provider,
                                 pack=pack, vol=vol, ch=ch, prev_gist=prev,
                                 arcs=vol_arcs or None)
            res_ch = _call(ctx_ch, "chapter", 2, vol_n=vol, ch=ch)
            if res_ch is not None and res_ch.ok:
                chapters_written += 1
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
    except KeyboardInterrupt:
        interrupted = True
        warnings.append("SIGINT：当前节点后落盘退出")
    finally:
        # ---- 幂等追加新卷 after_days → worldstate.pending（不覆盖 time/characters）----
        _append_roll_pending(ws, project_id, bp, vol)
        state.touch_stage(ws, project_id, state.stage or "built")
        append_transcript(ws, project_id, "roll.end", vol=vol, calls_used=calls_used,
                          chapters_written=chapters_written,
                          budget_exhausted=budget_exhausted, interrupted=interrupted,
                          warnings=warnings[:10])
        # 结果快照（F5e diff 基线 / 默认 rollback 点）
        from .snapshot import take_snapshot

        take_snapshot(ws, project_id, label=f"roll-ok-v{vol}")

    return RollResult(
        ok=not interrupted and not budget_exhausted and chapters_written > 0,
        project_id=project_id, vol=vol, calls_used=calls_used,
        chapters_written=chapters_written, arcs_written=arcs_written,
        budget_exhausted=budget_exhausted, budget_limit=max_calls,
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
