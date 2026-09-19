"""工具注册表与门禁协议（docs/07 §3）。

Agent 通过工具与系统交互；每个工具经注册表分派，并接受分级门禁 + 审计。
工具分级：safe / sensitive / danger（docs/05 §4.1）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .approval import ApprovalQueue, ApprovalRequest
from .errors import (
    DENIED,
    INTERNAL,
    NOT_FOUND,
    OK,
    SCHEMA_FAIL,
    NovelistError,
    SchemaFailError,
)
from .session import Budget, SessionInfo

LEVEL_SAFE = "safe"
LEVEL_SENSITIVE = "sensitive"
LEVEL_DANGER = "danger"

# 门禁处置（docs/07 §3.3）
APPROVAL_ALLOW = "allow"
APPROVAL_ASK = "ask"
APPROVAL_DENY = "deny"

# 合法处置值——**fail-closed** 判据（AG-6，2026-09-15 审计）：
# 策略文件里拼错的处置值（如 "alow"）以前会被 `check()` 原样返回，
# 而 `invoke()` 只在 `== DENY / == ASK` 时拦 → 拼错即**静默放行**。
_ALLOWED_DECISIONS = frozenset({APPROVAL_ALLOW, APPROVAL_ASK, APPROVAL_DENY})

# 判断型子代理（ADR-032 基座）允许的证据读取工具——只读，禁止写库/写文件的越权路径。
# 定义在 core 侧供 `ToolRegistry.list_defs(profile="evidence")` 使用（tools/__init__ 再导出）。
EVIDENCE_TOOL_NAMES = frozenset(
    {"read_file", "grep_text", "query_memory", "get_character_history", "get_plot_events",
     # M3ac-2（T-1）：结构化查询同样是只读取证，证据环与对话 agent 共用
     "list_chapters", "get_bible", "get_outline", "list_conflicts", "get_worldstate"}
)


class Tool:
    """一个受控工具（docs/07 §3.1 ToolDef）。"""

    def __init__(
        self,
        name: str,
        description: str,
        level: str,
        handler: Callable[..., Any],
        parameters: dict | None = None,
        required: list[str] | None = None,
        level_fn: Callable[[Any, dict], str | None] | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.level = level
        self.handler = handler
        self.parameters = parameters or {}
        # 必填参数名（AG-8）：以前 `parameters` 只是给 to_def 展示用、**从不校验**，
        # 模型少传/拼错键会一路走到 handler 里 KeyError → 只回 INTERNAL 无信息。
        self.required = list(required or [])
        # 按参数动态定级（2026-09-18）：写类工具可依目标路径收紧/放宽，见 filesys.write_file
        self.level_fn = level_fn

    def effective_level(self, session: Any, params: dict | None = None) -> str:
        """本次调用的有效分级。默认 `self.level`；`level_fn` 可依参数改写。

        **收紧容易放宽难**：`level_fn` 返回 None / 非法值 / 抛错都退回 `self.level`，
        绝不因判定失败而降到 safe（否则又是一条静默放行通道，同 AG-6 的教训）。
        """
        if self.level_fn is None:
            return self.level
        try:
            got = self.level_fn(session, params or {})
        except Exception:  # noqa: BLE001 - 定级失败按声明级别处理
            return self.level
        return got if got in (LEVEL_SAFE, LEVEL_SENSITIVE, LEVEL_DANGER) else self.level

    def invoke(self, session: SessionInfo, params: dict, budget: Budget | None = None) -> Any:
        try:
            return self.handler(session=session, params=params, budget=budget)
        except SchemaFailError:
            # 参数校验失败（SCHEMA_FAIL）
            raise

    def to_def(self) -> dict:
        """内部定义（含 `level` 权限字段，**仅供本系统内部使用**）。"""
        return {
            "name": self.name,
            "description": self.description,
            "level": self.level,
            "parameters": self.parameters,
            "required": self.required,
        }

    def to_openai_schema(self) -> dict:
        """下发模型用的 OpenAI function 工具定义（AG-2）。

        与 `to_def()` 的关键差别：① 包成 `{"type":"function","function":{...}}`；
        ② **不下发 `level`**——那是本系统的权限分级，模型不需要也不该知道；
        ③ `parameters` 包成合法 JSON Schema（`type=object` + `properties` + `required`）。
        """
        props = dict(self.parameters or {})
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": list(self.required),
                },
            },
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


def fail(code: str, data: Any = None, status: str = "error") -> ToolResult:
    """失败结果（AG-9）：**失败一律不带 `status="ok"`**。

    此前 `writing._write_draft` 等把异常包成 `ok(data={"error": ...})`，agent 从
    `status=ok` 无法分辨执行成败（模型据此以为已落盘）。
    """
    return ToolResult(status=status, code=code, data=data)


def denied(tool_name: str, reason: str = "", **extra: Any) -> ToolResult:
    """拒绝结果（AG-14）：权限拒绝应当是**可观察的返回值**，而不是异常。"""
    data: dict[str, Any] = {"tool": tool_name}
    if reason:
        data["reason"] = reason
    data.update(extra)
    return ToolResult(status="denied", code=DENIED, data=data)


class ToolRegistry:
    """工具注册表（docs/05/07）。

    - 门禁 `gate`：判定 safe/sensitive/danger 处置。
    - 审批 `approvals`（可选 ApprovalQueue）：`ask` 处置时入队并阻塞等待人工决策
      （docs/07 §3.3 wait + fallback deny-if-timeout）。
    - `decision_fn`（可选，优先于队列）：`ask` 时由外部回调即时决策
      （如 CLI 交互 input），回调签名 `(ApprovalRequest) -> "allow"|"deny"`。
      两者皆无时 `ask` 直接判 `denied`（AG-14：此前抛 DeniedError，异常穿透
      AgentRunner 会把整章打挂，而不是让模型看到"被拒绝"后改道）。
    """

    def __init__(
        self,
        gate: "PermissionGate | None" = None,
        approvals: "ApprovalQueue | None" = None,
        decision_fn: "Callable[[ApprovalRequest], str] | None" = None,
    ) -> None:
        self._tools: dict[str, Tool] = {}
        self._gate = gate or PermissionGate()
        self._approvals = approvals
        self._decision_fn = decision_fn

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        return self._tools[name]

    def select(self, profile: str | None = None) -> list[Tool]:
        """按权限面裁剪工具集（AG-19）。

        `profile` 取值：`None`/`"all"` = 全量；`"safe"`/`"sensitive"`/`"danger"` = 按级别；
        `"evidence"` = `EVIDENCE_TOOL_NAMES` 只读证据子集（ADR-032）。
        此前 `list_defs(profile)` 收下 `profile` 却**完全忽略**它——工具集无法按角色裁剪。
        """
        if not profile or profile == "all":
            return list(self._tools.values())
        if profile == "evidence":
            return [t for t in self._tools.values() if t.name in EVIDENCE_TOOL_NAMES]
        return [t for t in self._tools.values() if t.level == profile]

    def list_defs(self, profile: str | None = None) -> list[dict]:
        """内部定义列表（含 `level`）；`profile` 语义见 `select`。"""
        return [t.to_def() for t in self.select(profile)]

    def to_openai_schema(self, profile: str | None = None) -> list[dict]:
        """下发模型的原生工具定义（AG-2）；`profile` 语义见 `select`。"""
        return [t.to_openai_schema() for t in self.select(profile)]

    def invoke(
        self,
        session: SessionInfo,
        name: str,
        params: dict,
        budget: Budget | None = None,
        approval_timeout: float = 120.0,
    ) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return fail(NOT_FOUND, {"name": name, "available": sorted(self._tools)})
        # 门禁判定本身也可能抛（如策略 profiles 结构异常）——不能让它穿透成整章失败
        try:
            decision = self._gate.check(session, tool, params)
        except Exception as e:  # noqa: BLE001 - 门禁异常按拒绝处理（fail-closed）
            return denied(name, f"permission gate error: {type(e).__name__}: {e}")
        if decision == APPROVAL_DENY:
            return denied(name, "denied by permission profile")
        if decision == APPROVAL_ASK:
            req = self._approvals.submit(
                tool=tool.name, params=params, session=session, reason=f"tool {tool.name} requires approval"
            ) if self._approvals is not None else None
            if self._decision_fn is not None:
                verdict = self._decision_fn(req)  # 回调自行处理 req（可为 None）
            elif req is not None:
                verdict = self._approvals.wait_for_decision(req.id, timeout=approval_timeout)
            else:
                # 无审批通道：判拒绝而不是抛异常（AG-14）
                return denied(name, "requires approval but no approval channel (queue/decision_fn)")
            if verdict != "allow":
                return denied(name, "approval denied",
                              approval=req.id if req else None)
        # 参数校验（AG-8）：缺必填即回 SCHEMA_FAIL，并明确告诉模型缺哪个键
        missing = [k for k in tool.required if k not in (params or {})]
        if missing:
            return fail(SCHEMA_FAIL, {
                "error": f"missing required parameter(s): {', '.join(missing)}",
                "required": list(tool.required),
                "got": sorted((params or {}).keys()),
            })
        try:
            data = tool.invoke(session, params, budget)
            # 工具实现可直接返回 ToolResult（如用 ok() 构造）；此时不再二次包装，
            # 否则 result.data 会变成嵌套的 ToolResult 而不是业务数据。
            if isinstance(data, ToolResult):
                return data
            return ok(data=data)
        except NovelistError as e:
            return fail(e.code, {"error": e.message}, status=e.to_status())
        except Exception as e:  # noqa: BLE001 - 兜底为 INTERNAL
            # AG-8：带出异常类型与消息——此前 data=None，模型无法自纠、人也无法排查
            return fail(INTERNAL, {"error": f"{type(e).__name__}: {str(e)[:200]}"})


class PermissionGate:
    """门禁判定（docs/07 §3.3 / §3.4）。此处提供内存版构造；策略文件解析见 from_policy_file。

    判定语义（AG-6/AG-7，2026-09-15 审计后收紧）：
    - **fail-closed**：任何非 `allow/ask/deny` 的处置值（含拼错）都按 `deny` 处理并告警，
      不再"原样返回→invoke 只认 DENY/ASK→静默放行"。
    - **回退不炸**：请求的 profile 不存在 → 退 `supervised`；`supervised` 也缺 → 用
      内置兜底档（sensitive=ask / danger=deny）。此前 `profiles["supervised"]` 作为
      `dict.get` 默认值被**急切求值**，任何不含该段的策略文件都会让所有工具调用 KeyError。
    - 默认档把 `delete_file` 单独设为 `ask`（AG-10）：危险但可控，调用时人工确认；
      其余 danger 级仍 `deny`。
    """

    DEFAULT_PROFILE = "supervised"

    def __init__(self, profiles: dict[str, dict] | None = None) -> None:
        # profiles[name] -> {"sensitive": approval|..., "danger": ..., "tools": {name: ...}}
        self.profiles = profiles or {
            self.DEFAULT_PROFILE: {
                "sensitive": APPROVAL_ASK,
                "danger": APPROVAL_DENY,
                # AG-10：删文件属"危险但偶发必要"，交人工当场确认；非交互场景由
                # ToolRegistry 无审批通道时降级为 deny。
                "tools": {"delete_file": APPROVAL_ASK},
            },
        }

    @classmethod
    def from_policy_file(cls, path: str) -> "PermissionGate":
        """从 TOML 策略文件加载（docs/07 §3.4）。

        格式：
        [profile.supervised]
        sensitive = "ask"
        danger = "deny"
        [profile.supervised.tools]
        write_draft = "allow"
        """
        import tomllib

        from .errors import NovelistError as _NE

        with open(path, "rb") as f:
            data = tomllib.load(f)
        profiles: dict[str, dict] = {}
        for name, section in (data.get("profile") or {}).items():
            prof: dict = {}
            for key in ("safe", "sensitive", "danger"):
                if key in section:
                    # AG-6：解析期就校验取值，拼错立即报错（比运行期静默放行好得多）
                    if section[key] not in _ALLOWED_DECISIONS:
                        raise _NE(message=(
                            f"policy {path}: profile {name!r} 的 {key} = {section[key]!r} "
                            f"非法（只接受 {sorted(_ALLOWED_DECISIONS)}）"))
                    prof[key] = section[key]
            tools = section.get("tools") or {}
            if isinstance(tools, dict):
                for tname, tval in tools.items():
                    if tval not in _ALLOWED_DECISIONS:
                        raise _NE(message=(
                            f"policy {path}: profile {name!r} 的 tools.{tname} = {tval!r} "
                            f"非法（只接受 {sorted(_ALLOWED_DECISIONS)}）"))
                prof["tools"] = dict(tools)
            profiles[name] = prof
        return cls(profiles=profiles)

    def check(self, session: SessionInfo, tool: Tool, params: dict | None = None) -> str:
        """判定本次调用的处置。`params` 用于工具的路径感知定级（2026-09-18）。"""
        prof = self.profiles.get(session.permission_profile)
        if prof is None:  # 回退默认档（AG-7：不做急切求值，缺则用内置兜底）
            prof = self.profiles.get(self.DEFAULT_PROFILE)
        if prof is None:
            prof = {"sensitive": APPROVAL_ASK, "danger": APPROVAL_DENY}
        # 工具级覆盖优先
        tool_override = (prof.get("tools") or {}).get(tool.name)
        if tool_override:
            return self._settle(tool_override, tool.name, "tools override")
        level = tool.effective_level(session, params)
        if level == LEVEL_SAFE:
            return APPROVAL_ALLOW
        return self._settle(prof.get(level, APPROVAL_DENY), tool.name, f"level={level}")

    def _settle(self, decision: Any, tool_name: str, where: str) -> str:
        """合法处置原样返回；非法/缺失一律 deny 并告警（fail-closed）。"""
        if decision in _ALLOWED_DECISIONS:
            return str(decision)
        if decision is not None:
            from .output import emit

            emit(f"⚠ 权限策略取值非法（{where}，tool={tool_name}）：{decision!r} → 按 deny 处理")
        return APPROVAL_DENY
