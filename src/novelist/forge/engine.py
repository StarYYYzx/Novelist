"""递归构建引擎（docs/10 §7）：DFS 最小树 + 硬边界 + 增量落盘 + 续跑（M3l F1）。

F1 树形（**引擎确定性展开**，docs/08 F1 表格）：
```
book ── 设定骨架 + 卷主线一次出齐（写回蓝图 + 落 bible）
 └─ volume 1 ── 卷 1 主线细化 → outline/volumes.json
      └─ chapter 1-1 … chapter 1-K  ← 卷闸门：仅 vol=1 展开 chapter（B2 拍板）
 └─ volume 2 … volume N（vol≥2 只出卷主线，细纲留 `forge roll`）
```
模型的 `decide=expand/children`（arc/beat 层）F1 仅留痕到 warnings，F4 提供。

硬边界（docs/10 §7.3）：
- `max_calls` 分阶段配额（build 卷 1 默认 60）：到达即停，剩余节点不生成，报告标"预算耗尽"；
- `max_depth`（默认 4）：book=0 / volume=1 / chapter=2，超深强制不展开（本版树形天然受限）；
- `max_width`（默认 4）：本版宽度由 scale 确定性决定，参数保留、不截断章节（记录在案）；
- 每节点 `max_retries=1`：解析/校验失败重试一次，再失败**回退父层产物**，绝不静默跳过。

续跑：节点产物已存在（chapter 文件 / volumes.json 条目）即跳过；重跑同一节点覆盖写
（人物/伏笔按 id upsert，幂等）。SIGINT 在当前节点后落盘退出，被中断节点不落盘、
`calls_used` 不回退——`forge resume` 从该节点重跑。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..core.llm import ModerationBlockedError
from ..storage.workspace import Workspace
from . import genres as _genres
from .nodes import NodeContext, run_node, sync_bible, synthesize_worldstate, _load_json_list
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


def _node_label(kind: str, vol: int = 0, ch: int = 0) -> str:
    if kind == "chapter":
        return f"L3 chapter {vol}-{ch}"
    if kind == "volume":
        return f"L1 volume {vol}"
    return "L0 book"


def build(ws: Workspace, project_id: str, *, provider,
          max_calls: int = 60, max_depth: int = 4, max_width: int = 4,
          spec: Any = None, pack: dict | None = None,
          resume: bool = False,
          log_fn: Callable[[str], None] | None = None) -> BuildResult:
    """全权构建：book → volume(全卷) → chapter(仅 vol=1)，末尾 worldstate 确定性合成。

    `resume=True`：跳过已落盘节点（幂等续跑）。`log_fn` 缺省打印进度行
    `[calls/max] <node> … ok (calls=N)`。
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

    def _call(ctx: NodeContext, kind: str, depth: int, vol: int = 0, ch: int = 0):
        """执行一个节点（含 retry=1 与降级兜底）。返回 NodeResult | None（预算耗尽/失败跳过）。"""
        nonlocal calls_used, nodes_done
        label = _node_label(kind, vol, ch)
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
            _call(ctx, "book", 0)
            bp.save(ws, project_id)
            sync_bible(ws, project_id, bp)

        # ---- L1 volume（全部卷，卷主线一次出齐）----
        existing_vols = _load_json_list(ws, project_id, "outline/volumes.json")
        for vol in range(1, N + 1):
            if budget_exhausted:
                warnings.append(f"volume {vol}: 预算耗尽，未生成（卷主线不完整）")
                break
            if resume and any(x.get("vol") == vol for x in existing_vols):
                # 已落盘 → 跳过，但确保蓝图有该卷条目（续跑一致性）
                row = next(x for x in existing_vols if x.get("vol") == vol)
                _upsert_volume_from_row(bp, row)
                continue
            ctx = NodeContext(ws=ws, project_id=project_id, bp=bp, provider=provider,
                              pack=pack, spec=spec, vol=vol)
            res = _call(ctx, "volume", 1, vol=vol)
            if res is not None and res.ok:
                volumes_written += 1
            bp.save(ws, project_id)
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
                                     pack=pack, spec=spec, vol=1, ch=ch, prev_gist=prev)
                res_ch = _call(ctx_ch, "chapter", 2, vol=1, ch=ch)
                if res_ch is not None and res_ch.ok:
                    chapters_written += 1
                bp.save(ws, project_id)
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
        state.stage = "built" if not interrupted else "build"
        state.save(ws, project_id)
        append_transcript(ws, project_id, "build.end",
                          calls_used=calls_used, nodes_done=nodes_done,
                          chapters_written=chapters_written,
                          budget_exhausted=budget_exhausted,
                          interrupted=interrupted,
                          warnings=warnings[:10])

    ok = not interrupted and not budget_exhausted and chapters_written > 0
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
        blueprint_path=str(ws._abs(f"{project_id}/workspace/forge/blueprint.json")),  # noqa: SLF001
    )


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
