"""HTTP 服务（docs/07 §6.2，F8.3）。

FastAPI REST，封装 CLI/核心能力：项目状态、流水线推进、串行写章、导出、审批。

设计要点：
- 同一进程内建 ApprovalQueue（持久化到工作区 logs/），HTTP 与 CLI grant 可跨进程读写同一审批集。
- 写章默认用 `scripted` provider（无外部依赖）；业务接入真实模型见 `make_provider`。
- 认证：默认本地（可选简单 token）；docs/07 §6.2 "鉴权：本地 token / 简单会话"。
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query

from .config import load_config
from .core.approval import ApprovalQueue
from .core.errors import NovelistError
from .core.session import SessionInfo
from .core.tools import APPROVAL_ASK, PermissionGate
from .storage.checkpoint import Checkpoint
from .storage.workspace import Workspace, WorkspaceError
from .tools import build_registry

app = FastAPI(title="Novelist API", version="0.1.0")


def _workspace() -> Workspace:
    cfg = load_config()
    return Workspace(root=cfg.storage.workspace_root or ".")


def _locate(ws: Workspace) -> str:
    rp = Path(ws._abs(""))
    found = [d.name for d in rp.iterdir() if d.is_dir() and (d / "project.json").exists()]
    if not found:
        raise HTTPException(404, "no project found; run `novelist init` first")
    if len(found) > 1:
        raise HTTPException(400, f"multiple projects: {found}")
    return found[0]


# ---------- 头信息生成（HTTP 进程内审批队列） ----------
def _approval_queue(ws: Workspace, project_id: str) -> ApprovalQueue:
    # 跨进程：HTTP 端点从持久化文件重建队列，读 CLI/chapter 产生的待决审批
    return ApprovalQueue.load_persisted(persist_dir=ws._abs(f"{project_id}/logs"))


def _decision_fn(req):
    # HTTP 下 ask 处置默认入队等待（由 HTTP /grant 或 grant CLI 决策）
    return "wait"


@app.get("/projects")
def list_projects():
    ws = _workspace()
    rp = Path(ws._abs(""))
    names = [d.name for d in rp.iterdir() if d.is_dir() and (d / "project.json").exists()]
    return {"projects": names}


@app.get("/projects/{project_id}/status")
def project_status(project_id: str):
    ws = _workspace()
    ck = Checkpoint(ws)
    try:
        project = ck.restore(project_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"status failed: {e}") from e
    return project


@app.post("/projects/{project_id}/run")
def run_pipeline(project_id: str, to: str = Query("正文")):
    from .consistency import run_consistency
    from .core.pipeline import PIPELINE_STAGES, PipelineStateError, PipelineStateMachine

    ws = _workspace()
    ck = Checkpoint(ws)
    try:
        project = ck.restore(project_id)
        st = PipelineStateMachine()
        cur = project.get("pipeline_state") or "立项"
        want = to if to in PIPELINE_STAGES else "正文"
        # 直接跳到目标（HTTP 简化：允许一次性推进）
        if want != st.current:
            _advance(st, want)
    except PipelineStateError as e:
        raise HTTPException(400, f"cannot advance: {e}") from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"run failed: {e}") from e

    alerts = run_consistency(ws, project_id) if _ord(want) >= _ord("审查") else []
    project["pipeline_state"] = want
    ck.save(project_id, project)
    return {"stage": want, "consistency": [a.__dict__ for a in alerts]}


@app.post("/projects/{project_id}/chapters")
def write_chapter(project_id: str, vol: int = 1, ch: int = 1, provider: str = "scripted"):
    from .core.orchestrator import produce_chapter

    ws = _workspace()
    ck = Checkpoint(ws)
    try:
        ck.restore(project_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(404, f"project not found: {e}") from e

    prov = make_provider(provider)
    reg = build_registry(ws, gate=PermissionGate(), approvals=_approval_queue(ws, project_id),
                         decision_fn=lambda req: "deny")  # HTTP 写章默认 deny 审批
    sess = SessionInfo(project_id=project_id, agent="http")
    res = produce_chapter(ws, project_id, vol, ch, prov, session=sess, registry=reg,
                          prefer_direct=True, generation_tokens=200)
    if not res.ok:
        raise HTTPException(502, f"chapter failed: {res.result}")
    return {"draft_path": res.chapter_path, "events_committed": res.events_committed, "mode": res.mode}


@app.get("/projects/{project_id}/export")
def export_project(project_id: str, include_drafts: bool = False):
    from .core.export import export_project as _export

    ws = _workspace()
    text = _export(ws, project_id, include_drafts=include_drafts)
    import fastapi.responses as fr

    return fr.PlainTextResponse(text, media_type="text/markdown; charset=utf-8")


# ---------- 审批 ----------
@app.get("/pending-decisions")
def pending_decisions():
    ws = _workspace()
    project_id = _locate(ws)
    q = _approval_queue(ws, project_id)
    return {"pending": [{"id": r.id, "tool": r.tool, "reason": r.reason} for r in q.list_pending()]}


@app.post("/decisions/{request_id}")
def decide(request_id: str, approve: bool = True):
    ws = _workspace()
    project_id = _locate(ws)
    q = _approval_queue(ws, project_id)
    ok_ = q.decide(request_id, allow=approve)
    if not ok_:
        raise HTTPException(404, f"approval {request_id} not found")
    return {"id": request_id, "decision": "allow" if approve else "deny"}


# ---------- 辅助 ----------
def _ord(name: str) -> int:
    from .core.pipeline import PIPELINE_STAGES

    return PIPELINE_STAGES.index(name) if name in PIPELINE_STAGES else -1


def _advance(st, target: str) -> None:
    while st.current != target:
        stages = st.stages
        i = stages.index(st.current)
        st.advance(stages[i + 1])


def make_provider(provider: str):
    from .providers.fake import FakeProvider, ScriptedProvider

    if provider == "fake":
        return FakeProvider(reply="HTTP 生成占位文本。")
    if provider in ("scripted", "demo"):
        # HTTP 写章走 prefer_direct 直出：给一个直接返回文本的脚本
        return ScriptedProvider([
            {"final": "HTTP 生成的本章正文。\n苏晚立于山巅，望向远方的青冥山。"},
        ])
    if provider == "lmstudio":
        from .providers.lmstudio import LMStudioProvider

        return LMStudioProvider()
    if provider == "deepseek":
        from .providers.deepseek import DeepSeekProvider

        return DeepSeekProvider()
    from .providers.openai import OpenAICompatibleProvider

    return OpenAICompatibleProvider()
