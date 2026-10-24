"""流水线工具（DL-014，2026-10-24 拍板执行：G1 流水线工具化 + G3 枢纽层）。

M3ac 拍板 C（2026-09-19「流水线命令暂不包成 Tool」）经用户 2026-10-24 拍板反转：
对话 agent 是主交互主干（DL-002），开书/构建/写章必须能在对话里闭环完成——
这正是 DL-003 的后端完成线（M3ac-5）。本模块把这些动作包成受控工具：

- create_project（safe）：新建项目工作区——「开新书」的对话发生在项目存在之前
- start_book（sensitive）：一句话 → seed(auto) → 构建；smoke=True 只提炼+建蓝图
- build_blueprint（sensitive）：构建/续跑（审核放行后 resume=True 接着跑）
- approve_review（sensitive）：批准待审模块（复用 review.py，与 /fr-approve 同语义，非旁路）
- get_gaps（safe）：蓝图缺口清单——确定性引擎 detect_gaps 的对话出口。
  slots.py 原则「问题由引擎决定，候选由模型生成」的自然延伸：**缺口由引擎判定，
  问法与解析由 agent 承担**（替代 ask.py 八轮表单的对话式填设定）。
- write_chapter（sensitive）：写一章（完整管线档：事件循环 + 润色 + 回写）
- revise_module（sensitive）：按用户建议重生成待审模块（ADR-024 revise 通道）

工程约束：
- **provider 绑定才注册**：`tools(ws, provider=None)` 时本组不出现——章节生成内部
  构建的 registry 不传 provider，天然杜绝「生成中的子代理再开构建」的递归。
- 流水线工具自身的 LLM 消耗**不计入对话轮成本硬顶**（AgentRunner 只记自己发起的
  调用）；成本控制靠 sensitive 门禁（首次必问，G4）+ 工具结果里回带 calls_used。
- 长耗时操作复用既有可见性（output.emit / heartbeat，console 经 use_output 转发）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from ..core.tools import LEVEL_SAFE, LEVEL_SENSITIVE, SCHEMA_FAIL, Tool, fail, ok
from ..storage.workspace import Workspace


@dataclass
class PipelineContext:
    """流水线工具的共享上下文（console 持有，跨工具传递）。

    - `last_created_project`：create_project/start_book 建过的最新项目——console
      据此在枢纽对话结束后做「自动切入新项目 + 会话交接」（G3）。
    - `decision_fn`/`embedding`：透传给 write_chapter 内部的章节生成 registry，
      让生成期 sensitive 工具与对话层走同一条审批通道（同一个 G4 会话授权）。
    """

    decision_fn: Any = None
    embedding: Any = None
    last_created_project: str | None = None


def _new_project_id() -> str:
    """与 cli._new_project_id 同口径（proj-时间戳）。"""
    return "proj-" + time.strftime("%Y%m%d%H%M%S")


def _resolve_pid(session, params: dict) -> str | None:
    """项目定位：显式参数 > 会话绑定。枢纽态（session.project_id=None）必须显式给。"""
    pid = str(params.get("project_id") or "").strip() or getattr(session, "project_id", None)
    return pid or None


def _require_provider(provider) -> Any:
    if provider is None:
        raise RuntimeError("pipeline tool bound without provider（registry 装配缺 provider）")
    return provider


def _read_json(ws: Workspace, pid: str, rel: str):
    p = ws._abs(f"{pid}/{rel}")  # noqa: SLF001
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _next_chapter(ws: Workspace, pid: str) -> tuple[int, int] | None:
    """「下一章」的确定性计算：有细纲且未写（定稿/草稿都算已写）的最小 (vol, ch)。"""
    def _have(rel: str) -> set[tuple[int, int]]:
        d = ws._abs(f"{pid}/{rel}")  # noqa: SLF001
        out: set[tuple[int, int]] = set()
        if d.exists():
            for f in d.glob("*.md"):
                v, _, c = f.stem.partition("-")
                try:
                    out.add((int(v), int(c)))
                except ValueError:
                    continue
        return out

    outlined = _have("outline/chapters")
    written = _have("chapters") | _have("drafts/chapters")
    todo = sorted(outlined - written)
    return todo[0] if todo else None


def tools(ws: Workspace, provider=None, ctx: PipelineContext | None = None) -> list[Tool]:
    """装配流水线工具。`provider=None` 时返回空列表（见模块 docstring 的注册纪律）。"""
    if provider is None:
        return []
    pctx = ctx if ctx is not None else PipelineContext()

    # ---- create_project（safe：无成本、可 /delete 回收）----

    def _create_project(session, params, budget=None):
        from ..storage.checkpoint import Checkpoint

        title = str(params.get("title") or "").strip() or "（无题）"
        project_id = _new_project_id()
        try:
            ws.create_project(project_id)
        except Exception as e:  # noqa: BLE001 - WorkspaceError 等，转成可读失败
            return fail(SCHEMA_FAIL, {"error": f"create_project failed: {e}"})
        project = {
            "id": project_id,
            "title": title,
            "brief": "",
            "genre": None,
            "prefs": {},
            "budget": {},
            "pipeline_state": "立项",
            "event_seq": 0,
        }
        Checkpoint(ws).save(project_id, project)
        pctx.last_created_project = project_id
        return ok(data={"project_id": project_id, "title": title,
                        "dir": ws.project_dir(project_id).as_posix(),
                        "hint": "后续流水线工具都可用 project_id 指定这本书"})

    # ---- start_book（sensitive：seed 提炼 + 构建的真实 LLM 消耗）----

    def _start_book(session, params, budget=None):
        from ..forge.seed import run_seed

        pid = _resolve_pid(session, params)
        if not pid:
            return fail(SCHEMA_FAIL, {"error": "缺 project_id（枢纽态必须显式给；先 create_project）"})
        if pid not in ws.list_projects():
            return fail(SCHEMA_FAIL, {"error": f"项目 {pid} 不存在（先 create_project）"})
        brief = str(params.get("brief") or "").strip()
        if not brief:
            return fail(SCHEMA_FAIL, {"error": "缺 brief（一句话创意）"})
        smoke = bool(params.get("smoke"))
        kw: dict[str, Any] = {"provider": _require_provider(provider), "mode": "auto"}
        for key, cast in (("genre_pack", str), ("volumes", int),
                          ("chapters_per_volume", int)):
            if params.get(key) is not None:
                kw[key] = cast(params[key])
        if smoke:
            kw["smoke"] = True
        res = run_seed(ws, pid, brief, **kw)
        build = res.build or {}
        data: dict[str, Any] = {
            "ok": bool(res.ok), "project_id": pid, "smoke": smoke,
            "warnings": (res.warnings or [])[:8],
        }
        meta = _read_json(ws, pid, "workspace/forge/blueprint.json") or {}
        data["scale"] = (meta.get("meta") or {}).get("scale")
        data["title"] = (meta.get("meta") or {}).get("title")
        if smoke:
            data["hint"] = ("smoke 只提炼+建蓝图未构建；确认要点后 build_blueprint 开建，"
                            "缺口用 get_gaps 查")
            return ok(data=data)
        data.update({
            "gate_halted": bool(build.get("gate_halted")),
            "pending_review": build.get("pending_review") or [],
            "calls_used": build.get("calls_used"),
            "volumes_written": build.get("volumes_written"),
            "chapters_written": build.get("chapters_written"),
        })
        if build.get("gate_halted"):
            data["hint"] = ("构建暂停在审核闸门：把蓝图摘要念给用户（get_bible blueprint:<段>），"
                            "用户认可后 approve_review 放行，再 build_blueprint(resume=true) 续跑")
        return ok(data=data)

    # ---- build_blueprint（sensitive）----

    def _build_blueprint(session, params, budget=None):
        from ..forge.engine import build as forge_build

        pid = _resolve_pid(session, params)
        if not pid:
            return fail(SCHEMA_FAIL, {"error": "缺 project_id"})
        resume = bool(params.get("resume", True))
        kw: dict[str, Any] = {"provider": _require_provider(provider),
                              "resume": resume, "gate": True, "doctor": True}
        if params.get("max_calls") is not None:
            kw["max_calls"] = int(params["max_calls"])
        res = forge_build(ws, pid, **kw)
        data = {
            "ok": bool(res.ok), "project_id": pid, "resume": resume,
            "calls_used": res.calls_used, "nodes_done": res.nodes_done,
            "nodes_failed": res.nodes_failed,
            "volumes_written": res.volumes_written,
            "chapters_written": res.chapters_written,
            "gate_halted": res.gate_halted,
            "pending_review": res.pending_review,
            "warnings": (res.warnings or [])[:10],
        }
        if res.gate_halted:
            data["hint"] = ("待审模块经用户认可后 approve_review 放行，再本工具 resume=true 续跑")
        return ok(data=data)

    # ---- approve_review（sensitive：治理动作）----

    def _approve_review(session, params, budget=None):
        from ..forge.review import pending_modules, resolve_all_pending, resolve_pending

        pid = _resolve_pid(session, params)
        if not pid:
            return fail(SCHEMA_FAIL, {"error": "缺 project_id"})
        target = str(params.get("module") or "all").strip() or "all"
        remember = bool(params.get("remember"))
        pending = pending_modules(ws, pid)
        if not pending:
            return ok(data={"project_id": pid, "approved": [],
                            "remaining_pending": [],
                            "hint": "当前无待审模块（可能尚未构建，或已全部放行）"})
        if target == "all":
            approved = resolve_all_pending(ws, pid, remember=remember,
                                           note="agent approve_review")
        elif target in pending:
            resolve_pending(ws, pid, target, decision="approve", remember=remember,
                            note="agent approve_review")
            approved = [target]
        else:
            return fail(SCHEMA_FAIL, {"error": f"模块 {target!r} 不在待审清单",
                                      "pending": sorted(pending)})
        remaining = sorted(pending_modules(ws, pid))
        return ok(data={"project_id": pid, "approved": approved,
                        "remember": remember, "remaining_pending": remaining,
                        "hint": ("remember=true 已永久关闭这些模块的审核开关"
                                 if remember else "开关未动，下次构建同类产物仍会待审")})

    # ---- get_gaps（safe：纯确定性读）----

    def _get_gaps(session, params, budget=None):
        from ..forge import genres as _genres
        from ..forge.slots import detect_gaps, slots_for_genre
        from ..forge.state import Blueprint

        pid = _resolve_pid(session, params)
        if not pid:
            return fail(SCHEMA_FAIL, {"error": "缺 project_id"})
        if not ws._abs(f"{pid}/workspace/forge/blueprint.json").exists():  # noqa: SLF001
            return ok(data={"project_id": pid, "gaps": [], "count": 0,
                            "hint": "尚无蓝图（先 start_book）"})
        bp = Blueprint.load(ws, pid)
        try:
            pack = _genres.load_pack_for(bp.get("meta.template")) \
                if bp.get("meta.template") else None
        except KeyError:
            pack = None
        gaps = detect_gaps(bp, slots_for_genre(pack))
        rows = [{
            "key": g.slot.key, "label": g.slot.label, "level": g.slot.level,
            "reason": g.reason,  # unfilled | low_confidence
            "current": g.current if not isinstance(g.current, (dict, list)) else "…",
            "question": g.slot.ask, "default": g.slot.default,
            "options": g.slot.enum[:8] if g.slot.enum else [],
        } for g in gaps]
        return ok(data={"project_id": pid, "gaps": rows, "count": len(rows),
                        "hint": ("required 优先向用户确认；recommended 有推荐值，"
                                 "用户不介意就取默认并报备；答案用 update_blueprint 写入")})

    # ---- write_chapter（sensitive：单章真实 LLM 消耗）----

    def _write_chapter(session, params, budget=None):
        from ..core.approval import ApprovalQueue
        from ..core.orchestrator import produce_chapter
        from ..core.tools import PermissionGate
        from ..storage.checkpoint import Checkpoint
        from . import build_registry

        pid = _resolve_pid(session, params)
        if not pid:
            return fail(SCHEMA_FAIL, {"error": "缺 project_id"})
        vol, ch = params.get("vol"), params.get("ch")
        if vol is None or ch is None:
            nxt = _next_chapter(ws, pid)
            if nxt is None:
                return fail(SCHEMA_FAIL, {
                    "error": "没有可写的下一章（无细纲或已全部写完）；先 build_blueprint",
                    "hint": "也可显式给 vol/ch 重写指定章"})
            vol, ch = nxt
        try:
            vol_i, ch_i = int(vol), int(ch)
        except (TypeError, ValueError):
            return fail(SCHEMA_FAIL, {"error": "vol/ch 必须是整数"})
        if not ws._abs(f"{pid}/outline/chapters/{vol_i}-{ch_i}.md").exists():  # noqa: SLF001
            return fail(SCHEMA_FAIL, {"error": f"无 {vol_i}-{ch_i} 章细纲（先构建）",
                                      "vol": vol_i, "ch": ch_i})
        try:
            Checkpoint(ws).restore(pid)
        except Exception as e:  # noqa: BLE001
            return fail(SCHEMA_FAIL, {"error": f"项目状态读取失败: {e}"})
        approvals = ApprovalQueue(persist_dir=str(ws._abs(f"{pid}/logs")))  # noqa: SLF001
        reg = build_registry(ws, gate=PermissionGate(), approvals=approvals,
                             decision_fn=pctx.decision_fn, embedding=pctx.embedding)
        res = produce_chapter(
            ws, pid, vol_i, ch_i, _require_provider(provider),
            registry=reg, prefer_direct=True, generation_tokens=4000,
            embedding=pctx.embedding, inject_bible=True,
            # 完整管线档（《命令手册》推荐：事件循环 + 润色 + 逐事件回写）
            event_loop=True, polish=True,
            seam_review=True, volume_facts=True,
        )
        chars = 0
        if res.chapter_path:
            try:
                chars = len(res.chapter_path.read_text(encoding="utf-8"))
            except OSError:
                pass
        if not res.ok:
            return fail(SCHEMA_FAIL, {"error": f"chapter production failed (mode={res.mode})",
                                      "vol": vol_i, "ch": ch_i})
        return ok(data={
            "vol": vol_i, "ch": ch_i, "ok": True,
            "chapter_path": str(res.chapter_path), "chars": chars,
            "mode": res.mode, "attempts": res.attempts,
            "events_committed": res.events_committed,
            "events_revised": res.events_revised,
            "phase": res.phase,
            "hint": "草稿在 drafts/，转正走 publish（danger 级，须用户显式执行）",
        })

    # ---- revise_module（sensitive）----

    def _revise_module(session, params, budget=None):
        from ..forge.engine import revise_module as forge_revise

        pid = _resolve_pid(session, params)
        if not pid:
            return fail(SCHEMA_FAIL, {"error": "缺 project_id"})
        module = str(params.get("module") or "").strip()
        suggestions = str(params.get("instruction") or "").strip()
        if not module or not suggestions:
            return fail(SCHEMA_FAIL, {"error": "缺 module / instruction（用户的修改意见原话）"})
        try:
            diff = forge_revise(ws, pid, module, suggestions,
                                _require_provider(provider))
        except ValueError as e:
            from ..forge.review import REVIEW_MODULES

            return fail(SCHEMA_FAIL, {"error": str(e),
                                      "available": ["all"] + sorted(REVIEW_MODULES)})
        return ok(data={"project_id": pid, "module": module,
                        "diff": diff[:20],
                        "hint": "重生成后该模块仍 pending；用户看过新内容再 approve_review"})

    return [
        Tool("create_project", "新建小说项目工作区（开新书第一步；无 LLM 成本）",
             LEVEL_SAFE, _create_project,
             {"title": {"type": "string"}}, required=["title"]),
        Tool("start_book", "一句话创意 → 立项提炼 → 构建蓝图（mode=auto 不走表单）；"
                          "smoke=true 只提炼+建蓝图不构建（零构建成本，先看立项要素）",
             LEVEL_SENSITIVE, _start_book,
             {"brief": {"type": "string"}, "project_id": {"type": "string"},
              "genre_pack": {"type": "string"}, "volumes": {"type": "integer"},
              "chapters_per_volume": {"type": "integer"}, "smoke": {"type": "boolean"}},
             required=["brief"]),
        Tool("build_blueprint", "构建/续跑蓝图（书→卷→章细纲）；审核放行后 resume=true 接着跑",
             LEVEL_SENSITIVE, _build_blueprint,
             {"project_id": {"type": "string"}, "resume": {"type": "boolean"},
              "max_calls": {"type": "integer"}}),
        Tool("approve_review", "批准待审模块（module=all 批量；remember=true 同时永久关该模块审核）",
             LEVEL_SENSITIVE, _approve_review,
             {"project_id": {"type": "string"}, "module": {"type": "string"},
              "remember": {"type": "boolean"}}),
        Tool("get_gaps", "蓝图缺口清单（必填未填/低置信项）：对话式补设定的「该问什么」依据",
             LEVEL_SAFE, _get_gaps,
             {"project_id": {"type": "string"}}),
        Tool("write_chapter", "写一章（完整管线：事件循环+润色+回写；产草稿不自动转正）；"
                             "不给 vol/ch 自动写下一章",
             LEVEL_SENSITIVE, _write_chapter,
             {"project_id": {"type": "string"}, "vol": {"type": "integer"},
              "ch": {"type": "integer"}}),
        Tool("revise_module", "按用户修改意见重生成待审模块（如 worldview/characters/volumes）",
             LEVEL_SENSITIVE, _revise_module,
             {"project_id": {"type": "string"}, "module": {"type": "string"},
              "instruction": {"type": "string"}},
             required=["module", "instruction"]),
    ]
