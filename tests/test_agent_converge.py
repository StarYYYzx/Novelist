"""AG-13 条件收敛回归（`AgentRunner.converge()` + orchestrator 抢救分支）。

2026-09-18 批次 B：此前 `converge()` 在 src 只有 1 个调用点、tests **零覆盖**——
"轮次耗尽后抢救内容"这条关键路径完全没有回归保护：
是否真的下发 `tools=None`、证据里有没有 `kind=converge`、空响应会不会被当成成稿，全无人看守。

全部离线，用桩 provider，**不真调 LLM**。
"""

from __future__ import annotations

import pytest

from novelist.core.agent_runner import AgentLoopError, AgentRunner
from novelist.core.llm import LLMRequest, LLMResult, ToolCall, Usage
from novelist.core.orchestrator import produce_chapter
from novelist.core.session import SessionInfo
from novelist.core.tools import PermissionGate, Tool, ToolRegistry
from novelist.storage.checkpoint import Checkpoint
from novelist.storage.workspace import Workspace

CONVERGE_MARK = "不要再调用任何工具"


def _ws(tmp_path, pid="p"):
    ws = Workspace(root=str(tmp_path))
    ws.create_project(pid)
    Checkpoint(ws).save(pid, {"id": pid, "title": "t", "pipeline_state": "正文", "event_seq": 0})
    return ws, pid


def _sess(pid="p"):
    return SessionInfo(project_id=pid, agent="orchestrator")


class _Caps:
    tool_calling = True
    max_context = 8000
    json_mode = True
    streaming = False
    embedding = False


def _probe_tool():
    return Tool("probe", "探针", "safe", lambda session, params, budget=None: {"echo": params},
                {"q": {"type": "string"}}, required=["q"])


class _LoopForeverProvider:
    """一直回 tool_calls 直到收到收敛指令；`converge_text` 控制收敛时给什么内容。"""

    def __init__(self, converge_text: str = "", tool="probe"):
        self.reqs: list[LLMRequest] = []
        self._converge_text = converge_text
        self._tool = tool

    @property
    def capabilities(self):
        return _Caps()

    def complete(self, req: LLMRequest) -> LLMResult:
        self.reqs.append(req)
        last_user = ""
        for m in reversed(req.messages or []):
            if getattr(m, "role", None) == "user":
                last_user = getattr(m, "content", "") or ""
                break
        if CONVERGE_MARK in last_user:  # 收敛调用：不再给工具
            return LLMResult(ok=True, content=self._converge_text, finish_reason="stop",
                             provider="stub", usage=Usage(tokens_in=10, tokens_out=10))
        return LLMResult(ok=True, content="", finish_reason="tool_calls", provider="stub",
                         usage=Usage(tokens_in=10, tokens_out=10),
                         tool_calls=[ToolCall(id=f"c{len(self.reqs)}", name=self._tool,
                                              arguments={"q": "x"})])


# ------------------------------------------------------------ converge 单元

def test_converge_appends_instruction_and_disables_tools():
    """收敛调用必须明确"不得再调用工具"，并在请求里真的把 `tools` 关掉。"""
    reg = ToolRegistry(gate=PermissionGate({"supervised": {"safe": "allow"}}))
    reg.register(_probe_tool())
    prov = _LoopForeverProvider(converge_text="已完成的正文……")
    runner = AgentRunner(prov, _sess(), registry=reg, max_rounds=2)

    out = runner.converge("只给正文")

    assert out == "已完成的正文……"
    req = prov.reqs[-1]
    assert req.tools is None, "收敛调用不得再下发工具定义（否则模型又去调工具）"
    last_user = [m for m in req.messages if getattr(m, "role", None) == "user"][-1].content
    assert CONVERGE_MARK in last_user
    assert "只给正文" in last_user, "note 必须带进 prompt"


def test_converge_records_evidence():
    reg = ToolRegistry(gate=PermissionGate({"supervised": {"safe": "allow"}}))
    reg.register(_probe_tool())
    prov = _LoopForeverProvider(converge_text="抢救回来的正文")
    runner = AgentRunner(prov, _sess(), registry=reg, max_rounds=2)

    runner.converge()

    kinds = [e.get("kind") for e in runner.evidence]
    assert "converge" in kinds, f"证据轨迹缺 converge：{kinds}"


def test_converge_empty_content_returns_empty_string():
    """空响应必须是 `""`（可判空），不能是 None——否则 `if _conv` 与长度比较会分叉。"""
    reg = ToolRegistry(gate=PermissionGate({"supervised": {"safe": "allow"}}))
    prov = _LoopForeverProvider(converge_text="")
    runner = AgentRunner(prov, _sess(), registry=reg, max_rounds=2)
    assert runner.converge() == ""


# ------------------------------------------------------------ orchestrator 抢救分支

def test_loop_exhausted_without_draft_uses_converge(tmp_path):
    """无草稿 + 轮次耗尽 → 走收敛抢救；正文够长即按成功收尾并落盘。"""
    ws, pid = _ws(tmp_path)
    prov = _LoopForeverProvider(converge_text="这是收敛回来的整章正文内容，长度足够。" * 3)
    reg = ToolRegistry(gate=PermissionGate({"supervised": {"safe": "allow"}}))
    reg.register(_probe_tool())

    res = produce_chapter(ws, pid, 1, 1, prov, session=_sess(pid), registry=reg,
                          prefer_direct=False, max_rounds=2, direct_words_floor=20)

    assert res.ok, res.result
    draft = ws.draft_path(pid, 1, 1)
    assert draft.exists(), "收敛内容必须落盘，不能只在内存里"
    assert "收敛回来" in draft.read_text(encoding="utf-8")
    assert any("强制收敛" in f for f in res.soft_failures), res.soft_failures


def test_loop_exhausted_with_short_converge_is_failure(tmp_path):
    """收敛也拿不到足够内容 → 判失败（不得把 5 个字的"未完成清单"当正文）。"""
    ws, pid = _ws(tmp_path)
    prov = _LoopForeverProvider(converge_text="没写完")  # 3 字 < floor 20
    reg = ToolRegistry(gate=PermissionGate({"supervised": {"safe": "allow"}}))
    reg.register(_probe_tool())

    res = produce_chapter(ws, pid, 1, 1, prov, session=_sess(pid), registry=reg,
                          prefer_direct=False, max_rounds=2, direct_words_floor=20)

    assert not res.ok


def test_loop_exhausted_with_existing_draft_skips_converge(tmp_path):
    """已有草稿 → 直接用草稿，**不得**再发一次收敛调用（AG-13 拍板 A 的条件收敛）。"""
    ws, pid = _ws(tmp_path)
    draft = ws.draft_path(pid, 1, 1)
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("早已落盘的正文内容，长度足够通过下限检查。", encoding="utf-8")

    prov = _LoopForeverProvider(converge_text="绝不该用到的收敛内容")
    reg = ToolRegistry(gate=PermissionGate({"supervised": {"safe": "allow"}}))
    reg.register(_probe_tool())

    res = produce_chapter(ws, pid, 1, 1, prov, session=_sess(pid), registry=reg,
                          prefer_direct=False, max_rounds=2, direct_words_floor=20)

    assert res.ok
    assert not any(CONVERGE_MARK in (getattr(m, "content", "") or "")
                   for r in prov.reqs for m in (r.messages or [])), "有草稿就不该再收敛"
    assert any("以草稿为准" in f for f in res.soft_failures), res.soft_failures


def test_run_loop_raises_agent_loop_error_at_limit():
    """前置条件：持续回 tool_calls 确实会触发轮次上限（收敛分支的触发源）。"""
    reg = ToolRegistry(gate=PermissionGate({"supervised": {"safe": "allow"}}))
    reg.register(_probe_tool())
    prov = _LoopForeverProvider()
    runner = AgentRunner(prov, _sess(), registry=reg, max_rounds=2)
    with pytest.raises(AgentLoopError):
        runner.run_loop("目标")
