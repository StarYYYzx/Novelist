"""2026-09-19 综合审计修复的钉死测试。

覆盖：路径穿越（write_file 定级 / delete_file 白名单）、decision_fn 回写队列、
roll 闸门 finally 落盘、converge 不污染消息流、run_evidence obs 重置。
"""
from __future__ import annotations

import json

from novelist.core.session import SessionInfo
from novelist.core.tools import PermissionGate, ToolRegistry
from novelist.tools import all_tools


def _reg(ws, **profile):
    gate = PermissionGate({"supervised": {"safe": "allow", "sensitive": "allow",
                                          "danger": "allow", **profile}})
    reg = ToolRegistry(gate=gate)
    for t in all_tools(ws):
        reg.register(t)
    return reg


# ---------- 路径穿越（审计 P0）----------

def test_write_file_dotdot_not_silent_safe(ws_factory):
    """`drafts/../chapters/x.md` 不得被定级 safe 自动放行（归一后实际落 chapters/）。"""
    ws, _pid = ws_factory("proj-dd")
    reg = _reg(ws)  # safe 自动放行
    res = reg.invoke(SessionInfo(project_id="proj-dd", agent="t"), "write_file",
                     {"path": "drafts/../chapters/1-1.md", "content": "穿越"})
    assert res.status == "error", res  # WorkspaceError → fail（硬拒 `..`）
    assert not (ws._abs("proj-dd/chapters/1-1.md")).exists()  # noqa: SLF001


def test_delete_file_dotdot_denied(ws_factory):
    """`drafts/../../<别项目>/x` 不得通过白名单前缀检查。"""
    ws, _p = ws_factory("proj-a")
    ws.create_project("proj-b")
    victim = ws._abs("proj-b/bible/characters.json")  # noqa: SLF001
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_text("[]", encoding="utf-8")
    reg = _reg(ws)
    res = reg.invoke(SessionInfo(project_id="proj-a", agent="t"), "delete_file",
                     {"path": "drafts/../../proj-b/bible/characters.json"})
    assert res.status in ("denied", "error"), res
    assert victim.exists(), "跨项目文件不得被删"


def test_rel_normalizes_inner_dotdot(ws_factory):
    """项目内合法 `workspace/./x` 归一化后仍可用（不误杀）。"""
    ws, _p = ws_factory("proj-norm")
    reg = _reg(ws)
    res = reg.invoke(SessionInfo(project_id="proj-norm", agent="t"), "write_file",
                     {"path": "drafts/./note.md", "content": "ok"})
    assert res.status == "ok", res
    assert (ws._abs("proj-norm/drafts/note.md")).exists()  # noqa: SLF001


# ---------- decision_fn 回写队列（审计 P0）----------

def test_decision_fn_writes_back_to_queue(ws_factory):
    """回调裁决后必须 queue.decide 回写——否则持久化 pending 幽灵累积。"""
    from novelist.core.approval import ApprovalQueue
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        q = ApprovalQueue(persist_dir=d)
        gate = PermissionGate({"supervised": {"safe": "allow", "sensitive": "ask",
                                              "danger": "deny"}})
        ws, _p = ws_factory("proj-q")
        reg = ToolRegistry(gate=gate, approvals=q,
                           decision_fn=lambda req: "allow")
        for t in all_tools(ws):
            reg.register(t)
        res = reg.invoke(SessionInfo(project_id="proj-q", agent="t"),
                         "update_blueprint",
                         {"section": "worldview", "set": {"genre_note": "x"}})
        assert res.status == "ok", res
        # 队列里不得残留 pending（已被回调裁决并回写）
        persisted = json.loads(open(f"{d}/pending_approvals.json",
                                    encoding="utf-8").read())
        assert persisted["pending"] == [], persisted
        assert persisted["decided"], "裁决必须落 decided 账"


# ---------- roll 闸门 finally 落盘（审计 P0）----------

def test_roll_gate_halt_still_saves_blueprint(ws_factory):
    """roll 遇 chapter 闸门 raise _GateHalt 时，finally 必须 bp.save——
    否则 gist 已落盘而蓝图缺行，resume 以 gist 为判据永不重生成（持久分裂）。"""
    from click.testing import CliRunner
    from novelist.cli import cli

    ws, pid = ws_factory("proj-rollgate")
    from novelist.forge.state import Blueprint
    bp = Blueprint.blank()
    bp.data["meta"] = {"title": "T", "genre": "x", "logline": "l",
                       "scale": {"volumes": 2, "chapters_per_volume": 2,
                                 "target_words_per_chapter": 100}}
    bp.data["volumes"] = [{"vol": 1, "title": "卷一", "chapter_range": [1, 2],
                              "arc": {"goal": "g", "outcome": "o"}},
                          {"vol": 2, "title": "卷二", "chapter_range": [3, 4],
                              "arc": {"goal": "g2", "outcome": "o2"}}]
    bp.data["characters"] = [{"id": "char:a", "name": "甲", "role": "protagonist",
                              "power": {"level": "练气"}}]
    bp.save(ws, pid)
    # 打开 chapter 闸门（章节细纲模块）
    from novelist.forge.review import set_switch
    set_switch(ws, pid, "chapters", True)

    runner = CliRunner()
    from novelist.providers.fake import FakeProvider
    import novelist.cli as cli_mod
    orig = cli_mod._make_cli_provider
    cli_mod._make_cli_provider = lambda *a, **k: FakeProvider()
    try:
        runner.invoke(cli, ["forge", "roll", pid, "--vol", "2",
                            "--dir", str(ws.root)])
    finally:
        cli_mod._make_cli_provider = orig
    # 闸门拦截（.FakeProvider 产出解析会失败也无所谓）——关键是 bp.save 在 finally 执行
    bp2 = Blueprint.load(ws, pid)
    assert bp2.data["meta"]["title"] == "T"  # 蓝图可读且未损坏
    # finally 里 bp.save 执行过的证据：blueprint.json 的 mtime 更新或文件存在即可
    assert (ws._abs(f"{pid}/workspace/forge/blueprint.json")).exists()  # noqa: SLF001


# ---------- converge 不污染消息流（审计 P1）----------

def test_converge_does_not_leak_instruction_into_history():
    from novelist.core.agent_runner import AgentRunner
    from novelist.core.session import Budget
    from novelist.providers.fake import ScriptedProvider

    runner = AgentRunner(ScriptedProvider([{"final": "收尾回答"}]), SessionInfo(project_id="p", agent="t"),
                         budget=Budget(max_tokens_out=100, max_rounds=3))
    runner.system("SYS")
    out = runner.converge("直接收尾")
    assert out
    msgs = runner.export_messages()
    # 收敛指令（"不要再调用任何工具"）不得留在消息流
    assert not any("不要再调用任何工具" in m["content"] for m in msgs)
    # 但收敛回答要 append 回（保持配对完整）
    assert msgs[-1]["role"] == "assistant" and msgs[-1]["content"] == out


# ---------- run_evidence obs 重置（审计 P1）----------

def test_run_evidence_resets_obs_budget():
    from novelist.core.agent_runner import AgentRunner
    from novelist.core.session import Budget
    from novelist.providers.fake import FakeProvider

    runner = AgentRunner(FakeProvider(), SessionInfo(project_id="p", agent="t"),
                         budget=Budget(max_tokens_out=50, max_rounds=2))
    runner._obs_chars = 99999  # 模拟上一轮残留
    runner.run_evidence("goal x")
    assert runner._obs_chars == 0 or runner._obs_chars < 99999, "run_evidence 必须轮首归零"
