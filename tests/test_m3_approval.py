"""M3 门禁审批测试：ApprovalQueue、策略文件、ToolRegistry ask 处置、CLI grant。"""

import threading
import time

import pytest

from novelist.core.approval import ApprovalQueue
from novelist.core.session import SessionInfo
from novelist.core.tools import (
    APPROVAL_ASK,
    APPROVAL_DENY,
    APPROVAL_ALLOW,
    PermissionGate,
    Tool,
    ToolRegistry,
)
from novelist.core.errors import DENIED, DeniedError


def _sess(profile: str = "supervised") -> SessionInfo:
    return SessionInfo(project_id="p", agent="orchestrator", permission_profile=profile)


def _tool(name: str = "t1", level: str = "sensitive") -> Tool:
    return Tool(name=name, description="test", level=level, handler=lambda session, params, budget: {"v": params.get("v")})


# ---------- ApprovalQueue ----------


def test_approval_submit_decide_wait():
    q = ApprovalQueue()
    req = q.submit("t1", {"v": 1}, _sess(), "test reason")
    assert q.list_pending()[0].id == req.id
    assert q.decide(req.id, allow=True)
    assert q.wait_for_decision(req.id, timeout=1.0) == "allow"
    assert q.list_pending() == []


def test_approval_timeout_falls_back_deny():
    q = ApprovalQueue()
    req = q.submit("t1", {}, _sess(), "r")
    assert q.wait_for_decision(req.id, timeout=0.2) == "deny"  # 超时 → deny
    assert req.decision == "deny"


def test_approval_wait_unblocks_on_other_thread():
    q = ApprovalQueue()
    req = q.submit("t1", {}, _sess(), "r")
    result: list[str] = []

    def _waiter():
        result.append(q.wait_for_decision(req.id, timeout=5.0))

    t = threading.Thread(target=_waiter)
    t.start()
    time.sleep(0.1)
    assert result == []  # 尚未决策，仍在等待
    q.decide(req.id, allow=True)
    t.join(timeout=2.0)
    assert result == ["allow"]


def test_approval_persist_roundtrip(tmp_path):
    pd = str(tmp_path / "logs")
    q = ApprovalQueue(persist_dir=pd)
    req = q.submit("publish", {"ch": 3}, _sess(), "publish chapter")
    q2 = ApprovalQueue.load_persisted(pd)
    ids = [r.id for r in q2.list_pending()]
    assert req.id in ids
    assert q2.decide(req.id, allow=True)
    q3 = ApprovalQueue.load_persisted(pd)
    assert q3.list_pending() == []
    assert q3._decided.get(req.id) == "allow"


# ---------- PermissionGate.from_policy_file ----------


def test_policy_file_parsing(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text(
        """[profile.supervised]
sensitive = "ask"
danger = "deny"

[profile.supervised.tools]
write_draft = "allow"

[profile.auto]
sensitive = "deny"
danger = "deny"
""",
        encoding="utf-8",
    )
    gate = PermissionGate.from_policy_file(str(p))
    assert gate.check(_sess("supervised"), _tool("write_draft", "sensitive")) == APPROVAL_ALLOW  # 工具级覆盖
    assert gate.check(_sess("supervised"), _tool("promote_draft", "sensitive")) == APPROVAL_ASK
    assert gate.check(_sess("auto"), _tool("promote_draft", "sensitive")) == APPROVAL_DENY


# ---------- ToolRegistry ask 处置 ----------


def test_registry_ask_with_decision_fn_allow():
    gate = PermissionGate(profiles={"supervised": {"sensitive": APPROVAL_ASK, "danger": APPROVAL_DENY, "tools": {}}})
    reg = ToolRegistry(gate=gate, decision_fn=lambda req: "allow")
    reg.register(_tool("t1", "sensitive"))
    res = reg.invoke(_sess(), "t1", {"v": 42})
    assert res.status == "ok"
    assert res.data == {"v": 42}


def test_registry_ask_with_decision_fn_deny():
    gate = PermissionGate(profiles={"supervised": {"sensitive": APPROVAL_ASK, "danger": APPROVAL_DENY, "tools": {}}})
    reg = ToolRegistry(gate=gate, decision_fn=lambda req: "deny")
    reg.register(_tool("t1", "sensitive"))
    res = reg.invoke(_sess(), "t1", {"v": 42})
    assert res.status == "denied"
    assert res.code == DENIED


def test_registry_ask_with_queue_allow():
    gate = PermissionGate(profiles={"supervised": {"sensitive": APPROVAL_ASK, "danger": APPROVAL_DENY, "tools": {}}})
    q = ApprovalQueue()

    def _auto_allow(req):
        q.decide(req.id, allow=True)
        return "allow"

    reg = ToolRegistry(gate=gate, approvals=q, decision_fn=_auto_allow)
    reg.register(_tool("t1", "sensitive"))
    res = reg.invoke(_sess(), "t1", {"v": 1})
    assert res.status == "ok"


def test_registry_ask_no_approvals_raises():
    gate = PermissionGate(profiles={"supervised": {"sensitive": APPROVAL_ASK, "danger": APPROVAL_DENY, "tools": {}}})
    reg = ToolRegistry(gate=gate)
    reg.register(_tool("t1", "sensitive"))
    with pytest.raises(DeniedError):
        reg.invoke(_sess(), "t1", {"v": 1})


def test_registry_danger_denied_by_default():
    reg = ToolRegistry()  # supervised 默认：danger=deny
    reg.register(_tool("t1", "danger"))
    res = reg.invoke(_sess(), "t1", {"v": 1})
    assert res.status == "denied"
    assert res.code == DENIED


# ---------- CLI grant ----------


def test_cli_grant_list_and_approve(tmp_path):
    from click.testing import CliRunner
    from novelist.cli import cli
    from novelist.storage.checkpoint import Checkpoint
    from novelist.storage.workspace import Workspace

    ws = Workspace(root=str(tmp_path))
    pid = "proj-grant"
    ws.create_project(pid)
    Checkpoint(ws).save(pid, {"id": pid, "title": "grant-demo", "pipeline_state": "正文", "event_seq": 0})
    # 预置一条待决审批（模拟 chapter 期间产生的 ask）
    from novelist.core.approval import ApprovalQueue

    q = ApprovalQueue(persist_dir=ws._abs(f"{pid}/logs"))
    req = q.submit("promote_draft", {"vol": 1, "ch": 2}, _sess(), "promote draft")

    runner = CliRunner()
    r = runner.invoke(cli, ["grant", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert req.id in r.output

    r2 = runner.invoke(cli, ["grant", str(tmp_path), "--approve", req.id])
    assert r2.exit_code == 0, r2.output
    assert "allow" in r2.output

    r3 = runner.invoke(cli, ["grant", str(tmp_path)])
    assert "no pending approvals" in r3.output
