"""工具注册表与门禁协议（docs/07 §3）。

Agent 通过工具与系统交互；每个工具经注册表分派，并接受分级门禁 + 审计。
工具分级：safe / sensitive / danger（docs/05 §4.1）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .errors import DENIED, INTERNAL, NOT_FOUND, OK, DeniedError, NovelistError, SchemaFailError
from .session import Budget, SessionInfo

LEVEL_SAFE = "safe"
LEVEL_SENSITIVE = "sensitive"
LEVEL_DANGER = "danger"

# 门禁处置（docs/07 §3.3）
APPROVAL_ALLOW = "allow"
APPROVAL_ASK = "ask"
APPROVAL_DENY = "deny"


class Tool:
    """一个受控工具（docs/07 §3.1 ToolDef）。"""

    def __init__(
        self,
        name: str,
        description: str,
        level: str,
        handler: Callable[..., Any],
        parameters: dict | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.level = level
        self.handler = handler
        self.parameters = parameters or {}

    def invoke(self, session: SessionInfo, params: dict, budget: Budget | None = None) -> Any:
        try:
            return self.handler(session=session, params=params, budget=budget)
        except SchemaFailError:
            # 参数校验失败（SCHEMA_FAIL）
            raise

    def to_def(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "level": self.level,
            "parameters": self.parameters,
        }


@dataclass
class ToolResult:
    """统一工具结果（docs/07 §3.2）。"""

    status: str  # ok | denied | error
    code: str  # 顶层错误码（docs/07 §9）
    data: Any = None
    usage: dict[str, Any] = field(default_factory=dict)
    audit_id: str | None = None


def ok(data: Any = None, usage: dict | None = None) -> ToolResult:
    return ToolResult(status="ok", code=OK, data=data, usage=usage or {})


class ToolRegistry:
    """工具注册表（docs/05/07）。"""

    def __init__(self, gate: "PermissionGate | None" = None) -> None:
        self._tools: dict[str, Tool] = {}
        self._gate = gate or PermissionGate()

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        return self._tools[name]

    def list_defs(self, profile: str | None = None) -> list[dict]:
        return [t.to_def() for t in self._tools.values()]

    def invoke(self, session: SessionInfo, name: str, params: dict, budget: Budget | None = None) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(status="error", code=NOT_FOUND, data={"name": name})
        decision = self._gate.check(session, tool)
        if decision == APPROVAL_DENY:
            return ToolResult(status="denied", code=DENIED, data={"tool": name})
        if decision == APPROVAL_ASK:
            raise DeniedError(f"tool '{name}' requires approval (ask)")  # 进一步接入人机审批系统
        try:
            data = tool.invoke(session, params, budget)
            return ok(data=data)
        except NovelistError as e:
            return ToolResult(status=e.to_status(), code=e.code, data={"error": e.message})
        except Exception:  # noqa: BLE001 - 兜底为 INTERNAL
            return ToolResult(status="error", code=INTERNAL)


class PermissionGate:
    """门禁判定（docs/07 §3.3 / §3.4）。此处提供内存版构造；策略文件解析见 config。"""

    def __init__(self, profiles: dict[str, dict] | None = None) -> None:
        # profiles[name] -> {"sensitive": approval|..., "danger": ..., "tools": {name: ...}}
        self.profiles = profiles or {
            "supervised": {"sensitive": APPROVAL_ASK, "danger": APPROVAL_DENY, "tools": {}},
        }

    def check(self, session: SessionInfo, tool: Tool) -> str:
        prof = self.profiles.get(session.permission_profile, self.profiles["supervised"])
        # 工具级覆盖优先
        tool_override = (prof.get("tools") or {}).get(tool.name)
        if tool_override:
            return tool_override
        if tool.level == LEVEL_SAFE:
            return APPROVAL_ALLOW
        return prof.get(tool.level, APPROVAL_DENY)
