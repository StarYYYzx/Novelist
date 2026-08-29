"""错误码（docs/07 §9 / §9.1）。映射 HTTP 与工具 result.status。"""

from __future__ import annotations

from dataclasses import dataclass

# 顶层错误码（docs/07 §9）
OK = "OK"
DENIED = "DENIED"
NOT_FOUND = "NOT_FOUND"
BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
SCHEMA_FAIL = "SCHEMA_FAIL"
PROVIDER_ERROR = "PROVIDER_ERROR"
MODERATION_BLOCKED = "MODERATION_BLOCKED"
INTERNAL = "INTERNAL"

# 错误码 -> HTTP 状态（docs/07 §9.1 映射表）
HTTP_STATUS: dict[str, int] = {
    OK: 200,
    DENIED: 403,
    NOT_FOUND: 404,
    BUDGET_EXCEEDED: 429,
    SCHEMA_FAIL: 422,
    PROVIDER_ERROR: 502,
    MODERATION_BLOCKED: 451,
    INTERNAL: 500,
}


@dataclass
class NovelistError(Exception):
    code: str = INTERNAL
    message: str = "internal error"

    def to_status(self) -> str:
        return "denied" if self.code == DENIED else "error"


# 便捷子类
class DeniedError(NovelistError):
    def __init__(self, message: str = "permission denied") -> None:
        super().__init__(code=DENIED, message=message)


class NotFoundError(NovelistError):
    def __init__(self, message: str = "not found") -> None:
        super().__init__(code=NOT_FOUND, message=message)


class BudgetExceededError(NovelistError):
    def __init__(self, message: str = "budget exceeded") -> None:
        super().__init__(code=BUDGET_EXCEEDED, message=message)


class SchemaFailError(NovelistError):
    def __init__(self, message: str = "schema validation failed") -> None:
        super().__init__(code=SCHEMA_FAIL, message=message)


class ProviderError(NovelistError):
    def __init__(self, message: str = "provider error") -> None:
        super().__init__(code=PROVIDER_ERROR, message=message)


class InternalError(NovelistError):
    def __init__(self, message: str = "internal error") -> None:
        super().__init__(code=INTERNAL, message=message)
