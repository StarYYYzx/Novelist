"""工具集（docs/05 §4 / docs/07 §3）。

Agent 通过受控工具与系统交互。本包把各领域工具装配进 ToolRegistry（safe/sensitive/danger 分级，
受 PermissionGate 门禁 + 审计）。
"""

from __future__ import annotations

from ..core.session import SessionInfo
from ..core.tools import PermissionGate, Tool, ToolRegistry
from ..storage.workspace import Workspace

from . import writing  # noqa: F401  装配副作用（注册工具）
from . import filesys  # noqa: F401
from . import memory_tools  # noqa: F401
from . import governance  # noqa: F401


def build_registry(
    ws: Workspace,
    gate: PermissionGate | None = None,
    approvals: "ApprovalQueue | None" = None,
    decision_fn=None,
    embedding=None,
) -> ToolRegistry:
    """装配完整工具注册表（docs/05 §4.1 / docs/07 §3.3）。

    approvals/decision_fn 透传给 ToolRegistry（ask 处置的人工审批接入，docs/07 §3.3）。
    embedding 透传给记忆工具（docs/07 §7.1）；None 时检索降级为关键词索引（F9.4）。
    """
    reg = ToolRegistry(gate=gate or PermissionGate(), approvals=approvals, decision_fn=decision_fn)
    for tool in all_tools(ws, embedding=embedding):
        reg.register(tool)
    return reg


def all_tools(ws: Workspace, embedding=None) -> list[Tool]:
    return (
        filesys.tools(ws)
        + writing.tools(ws)
        + memory_tools.tools(ws, embedding=embedding)
        + governance.tools(ws)
    )


# 判断型子代理（ADR-032 基座）允许的证据读取工具——只读，禁止写库/写文件的越权路径。
EVIDENCE_READ_TOOLS = frozenset(
    {"read_file", "grep_text", "query_memory", "get_character_history", "get_plot_events"}
)


def evidence_tools(ws: Workspace, embedding=None) -> list[Tool]:
    """取 `all_tools` 中允许的证据读取子集（只读，供 chronicler/reviewer 取证）。"""
    return [t for t in all_tools(ws, embedding=embedding) if t.name in EVIDENCE_READ_TOOLS]


def evidence_registry(
    ws: Workspace,
    gate: PermissionGate | None = None,
    embedding=None,
) -> ToolRegistry:
    """装配只读证据 registry（ADR-032）：仅 `EVIDENCE_READ_TOOLS`，供子代理读圣经/记忆/草稿取证。"""
    reg = ToolRegistry(gate=gate or PermissionGate())
    for tool in evidence_tools(ws, embedding=embedding):
        reg.register(tool)
    return reg


__all__ = ["build_registry", "all_tools", "evidence_registry", "evidence_tools",
           "PermissionGate", "SessionInfo"]
