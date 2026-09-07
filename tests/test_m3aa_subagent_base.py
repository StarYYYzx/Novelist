"""M3aa 批次：子代理基座（ADR-032 阶段 F0）单测。

验证 group：证据循环 run_evidence / thinking 与 response_format 透传 / 证据轨迹 /
只读证据 registry / 与既有 run_loop 向后兼容。全部用确定性 Provider，绝不真调 LLM（docs/09 §2.1）。
"""

from __future__ import annotations

import pytest

from novelist.core.agent_runner import AgentLoopError, AgentRunner
from novelist.core.llm import LLMMessage, LLMRequest, LLMResult, ToolCall, Usage
from novelist.core.session import SessionInfo
from novelist.core.tools import PermissionGate, Tool, ToolRegistry
from novelist.providers.fake import ScriptedProvider
from novelist.storage.workspace import Workspace
from novelist.tools import EVIDENCE_READ_TOOLS, evidence_registry, evidence_tools


def _ws(tmp_path) -> Workspace:
    return Workspace(root=str(tmp_path))


def _tool() -> Tool:
    return Tool(
        "probe",
        "测试工具：返回固定文本",
        "safe",
        lambda session, params, budget=None: {"probe": "stone"},
        {"q": {"type": "string"}},
    )


class SpyProvider:
    """记录每次请求的 thinking/response_format，并按脚本决策（ScriptedProvider 行为）。"""

    def __init__(self, script: list[dict]) -> None:
        self._inner = ScriptedProvider(script)
        self.seen: list[tuple[bool | None, str]] = []

    @property
    def capabilities(self):
        return self._inner.capabilities

    def complete(self, req: LLMRequest) -> LLMResult:
        self.seen.append((req.thinking, req.response_format))
        return self._inner.complete(req)


def _session(pid: str = "proj-x") -> SessionInfo:
    return SessionInfo(project_id=pid, agent="subagent")


# ---- 证据循环：正常路径 ----
def test_run_evidence_returns_final_and_trail(tmp_path):
    reg = ToolRegistry(gate=PermissionGate())
    reg.register(_tool())
    provider = ScriptedProvider([{"tool": "probe", "args": {"q": "?"}}, {"final": '{"bound": true}'}])
    runner = AgentRunner(provider, _session(), registry=reg, max_rounds=5)
    run = runner.run_evidence("抽查证据", system_prompt="你是审校师", response_format="json_object")
    assert run.final == '{"bound": true}'
    assert run.rounds == 2
    kinds = [e["kind"] for e in run.evidence]
    assert kinds == ["decide", "tool", "decide", "final"]
    assert run.evidence[1]["tool"] == "probe"
    assert run.evidence[1]["ok"] == "OK"


def test_run_loop_backward_compat(tmp_path):
    """既有 run_loop 语义不变：返回最终文本。"""
    reg = ToolRegistry(gate=PermissionGate())
    reg.register(_tool())
    provider = ScriptedProvider([{"tool": "probe", "args": {}}, {"final": "done"}])
    runner = AgentRunner(provider, _session(), registry=reg)
    assert runner.run_loop("目标") == "done"


def test_thinking_and_rf_threaded(tmp_path):
    """ADR-032 基座：thinking/response_format 按调用透传进 LLMRequest。"""
    provider = SpyProvider([{"final": "ok"}])
    runner = AgentRunner(provider, _session(), thinking=True)
    runner.run_evidence("审", response_format="json_object")
    thinking, rf = provider.seen[0]
    assert thinking is True
    assert rf == "json_object"


def test_thinking_default_none_threaded(tmp_path):
    """缺省 thinking=None（跟随 Provider），不破坏现有关思考的编辑 Agent。"""
    provider = SpyProvider([{"final": "ok"}])
    runner = AgentRunner(provider, _session())
    runner.run_evidence("审")
    assert provider.seen[0][0] is None


def test_system_prompt_injected(tmp_path):
    provider = ScriptedProvider([{"final": "ok"}])
    runner = AgentRunner(provider, _session())
    runner.run_evidence("审", system_prompt="侦探人设")
    sys_msgs = [m for m in runner._messages if m.role == "system"]
    assert sys_msgs and sys_msgs[0].content == "侦探人设"


def test_round_limit_raises(tmp_path):
    """只读证据但不到 final -> 达上限强制收敛。"""
    reg = ToolRegistry(gate=PermissionGate())
    reg.register(_tool())
    provider = ScriptedProvider([{"tool": "probe", "args": {}}] * 10)
    runner = AgentRunner(provider, _session(), registry=reg, max_rounds=2)
    with pytest.raises(AgentLoopError):
        runner.run_evidence("停不下来")


def test_no_registry_tool_raises(tmp_path):
    provider = ScriptedProvider([{"tool": "probe", "args": {}}])
    runner = AgentRunner(provider, _session())
    with pytest.raises(AgentLoopError):
        runner.run_evidence("要工具但没绑定")


def test_evidence_property(tmp_path):
    provider = ScriptedProvider([{"final": "ok"}])
    runner = AgentRunner(provider, _session())
    runner.run_evidence("快速核查")
    assert runner.evidence and runner.evidence[-1]["kind"] == "final"


# ---- 只读证据 registry ----
def test_evidence_registry_only_read_tools(tmp_path):
    reg = evidence_registry(_ws(tmp_path))
    names = reg._tools.keys()
    assert set(names) == set(EVIDENCE_READ_TOOLS)
    assert "write_file" not in names
    assert "write_draft" not in names


def test_evidence_tools_subset(tmp_path):
    names = {t.name for t in evidence_tools(_ws(tmp_path))}
    assert names == set(EVIDENCE_READ_TOOLS)
    assert "reindex_memory" not in names  # sensitive 写索引工具被排除