"""人工审批队列（docs/07 §3.3，F6.1，ADR-007）。

门禁判定为 `ask` 的工具调用进入审批队列，等待人工决策（approve/deny），
超时回退 deny（`fallback deny-if-timeout`）。

- 单进程内存实现：CLI 交互（grant 命令）与 HTTP 审批端点共用同一队列。
- 决策来源：`decide(id, allow)` 由 CLI/HTTP/策略注入。
- 同步等待：`wait_for_decision(id, timeout)` 阻塞至决策或超时。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PersistedSessionRef:
    """从磁盘恢复的审批请求所携带的 session 占位（AG-23，2026-09-15 审计）。

    审批队列的**跨进程**用途只有"展示 + 裁决"，那时只需要 agent 名；真实调用工具需要
    `project_id`，而它在落盘时没有保存。此前用匿名 `type("S", (), {...})()` 伪造 session，
    误用时只会抛一句难以定位的 AttributeError；现在是有名类型 + 明确注解。
    """

    agent: str | None = None
    persisted: bool = True


@dataclass
class ApprovalRequest:
    """一条待人工决策的请求。"""

    id: str
    tool: str
    params: dict[str, Any]
    session: Any  # SessionInfo
    reason: str
    created_at: float = field(default_factory=time.time)
    decision: str | None = None  # "allow" | "deny" | None(待决)


class ApprovalQueue:
    """人工审批队列（线程安全，threading.Condition 通知等待者）。

    可选 `persist_dir`：把 pending/已决请求同步到 `<dir>/pending_approvals.json`，
    供独立进程（CLI `grant`、HTTP 服务）读写同一审批集。
    """

    def __init__(self, persist_dir: str | None = None) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._pending: dict[str, ApprovalRequest] = {}
        self._decided: dict[str, str] = {}  # id -> decision
        self._history: list[dict] = []
        self._persist_dir = persist_dir

    # ---- 持久化 ----
    def _persist_path(self) -> str | None:
        if not self._persist_dir:
            return None
        import os

        return os.path.join(self._persist_dir, "pending_approvals.json")

    def _write_persist(self) -> None:
        path = self._persist_path()
        if not path:
            return
        import json
        import os

        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = {
            "pending": [
                {
                    "id": r.id, "tool": r.tool, "params": r.params,
                    "agent": getattr(r.session, "agent", None),
                    "reason": r.reason, "created_at": r.created_at,
                }
                for r in self._pending.values()
            ],
            "decided": [
                {"id": k, "decision": v, "ts": time.time()} for k, v in self._decided.items()
            ],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    @classmethod
    def load_persisted(cls, persist_dir: str) -> "ApprovalQueue":
        """从持久化文件重建队列（供独立进程 grant/server 读取）。"""
        import json
        import os

        q = cls(persist_dir=persist_dir)
        path = os.path.join(persist_dir, "pending_approvals.json")
        if not os.path.exists(path):
            return q
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for p in data.get("pending", []):
            req = ApprovalRequest(
                id=p["id"], tool=p["tool"], params=p.get("params", {}),
                session=PersistedSessionRef(agent=p.get("agent")),
                reason=p.get("reason", ""), created_at=p.get("created_at", 0.0),
            )
            q._pending[req.id] = req
        for d in data.get("decided", []):
            q._decided[d["id"]] = d["decision"]
        return q

    def submit(self, tool: str, params: dict, session: Any, reason: str) -> ApprovalRequest:
        req = ApprovalRequest(
            id=f"apv:{uuid.uuid4().hex[:10]}",
            tool=tool,
            params=params,
            session=session,
            reason=reason,
        )
        with self._cond:
            self._pending[req.id] = req
            self._cond.notify_all()
            self._write_persist()
        return req

    def list_pending(self) -> list[ApprovalRequest]:
        with self._cond:
            return list(self._pending.values())

    def decide(self, req_id: str, allow: bool) -> bool:
        """人工决策：allow=True 批准，allow=False 拒绝。返回是否找到该请求。"""
        with self._cond:
            req = self._pending.get(req_id)
            if req is None:
                return False
            req.decision = "allow" if allow else "deny"
            self._decided[req.id] = req.decision
            self._history.append(
                {
                    "id": req.id,
                    "tool": req.tool,
                    "decision": req.decision,
                    "ts": time.time(),
                }
            )
            del self._pending[req_id]
            self._cond.notify_all()
            self._write_persist()
            return True

    def wait_for_decision(self, req_id: str, timeout: float = 120.0) -> str:
        """阻塞等待决策；超时回退 deny（docs/07 §3.3 fallback deny-if-timeout）。

        AG-15（2026-09-15 审计）：超时判定必须**与裁决同样落盘并入历史**——此前超时分支
        只在内存里标记 deny，不写 `pending_approvals.json`、不记 `history`，于是
        ① 另一个进程（`novelist grant`）看到的仍是 pending，可能"批准"一条已被判 deny 的请求；
        ② 事后审计看不到这条拒绝。
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                if req_id in self._decided:
                    return self._decided[req_id]
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # 超时 → 锁内直接标记 deny（不重入 decide() 以避免不可重入 Condition 死锁）
                    req = self._pending.get(req_id)
                    if req is not None:
                        req.decision = "deny"
                        self._pending.pop(req_id, None)
                    self._decided[req_id] = "deny"
                    self._history.append({
                        "id": req_id,
                        "tool": getattr(req, "tool", None),
                        "decision": "deny",
                        "reason": "timeout",
                        "ts": time.time(),
                    })
                    self._write_persist()
                    self._cond.notify_all()
                    return "deny"
                self._cond.wait(remaining)

    def history(self) -> list[dict]:
        with self._cond:
            return list(self._history)
