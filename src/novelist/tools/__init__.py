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


def build_registry(ws: Workspace, gate: PermissionGate | None = None) -> ToolRegistry:
    """装配完整工具注册表（docs/05 §4.1 / docs/07 §3.3）。"""
    reg = ToolRegistry(gate=gate or PermissionGate())
    for tool in all_tools(ws):
        reg.register(tool)
    return reg


def all_tools(ws: Workspace) -> list[Tool]:
    return (
        filesys.tools(ws)
        + writing.tools(ws)
        + memory_tools.tools(ws)
    )


__all__ = ["build_registry", "all_tools", "PermissionGate", "SessionInfo"]
