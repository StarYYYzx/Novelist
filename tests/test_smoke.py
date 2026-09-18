"""冒烟测试：验证脚手架核心桩可导入并具备最小正确行为。

覆盖（docs/09 §2）：核心工具、状态机、事件、围读会、门禁、SQLite 索引、FakeProvider。
"""

from __future__ import annotations

import pytest

from novelist.core.events import EventBus
from novelist.core.llm import LLMMessage, LLMRequest
from novelist.core.pipeline import PipelineStateMachine, PipelineStateError
from novelist.core.moderation import ModerationPrechecker
from novelist.core.scene import SceneBus
from novelist.core.tools import PermissionGate, Tool, ToolRegistry
from novelist.core.session import SessionInfo
from novelist.providers.fake import FakeProvider


def test_fake_provider_returns_reply_and_tracks_usage():
    p = FakeProvider(reply="第一章草稿")
    res = p.complete(LLMRequest(messages=[LLMMessage(role="user", content="写一章")]))
    assert res.ok
    assert res.content == "第一章草稿"
    assert res.usage is not None and res.usage.tokens_out == 10


def test_fake_provider_can_simulate_moderation_block():
    p = FakeProvider(blocked=True, block_reason="safety")
    res = p.complete(LLMRequest(messages=[LLMMessage(role="user", content="写冲突")]))
    assert res.ok is False
    assert res.blocked is True
    assert res.block_reason == "safety"


def test_event_bus_publishes_monotonic_seq():
    bus = EventBus()
    seen = []
    bus.subscribe(lambda ev: seen.append(ev.kind))
    bus.publish("pipeline.step", payload={"stage": "大纲"})
    bus.publish("agent.tool", payload={"tool": "write_draft"})
    assert seen == ["pipeline.step", "agent.tool"]


def test_pipeline_state_machine_advance_and_revision():
    st = PipelineStateMachine()
    assert st.current == "立项"  # 起始阶段为 stages[0]
    st.advance("世界观")
    assert st.current == "世界观"
    # 修订回流：无论当前阶段都允许进入"修订"并可从修订续跑
    st.revert_to_revision()
    assert st.current == "修订"
    st.resume_from_revision("大纲")
    assert st.current == "大纲"
    # 非法跳步（倒退/未知阶段）
    with pytest.raises(PipelineStateError):
        st.advance("立项")  # 当前=大纲，回退到立项非法
    with pytest.raises(PipelineStateError):
        st.resume_from_revision("不存在的阶段")


def test_scene_bus_join_say_leave_and_all_left_close():
    bus = SceneBus()
    bus.create("sc:1", topic="对峙", actors=["a", "b"], max_rounds=3)
    assert bus.join("sc:1", "a")["ok"]
    assert bus.join("sc:1", "b")["ok"]
    assert bus.say("sc:1", "a", "你欠我一条命。")["ok"]
    bus.leave("sc:1", "a")
    bus.leave("sc:1", "b")  # 全部离场 -> 自动 close
    assert bus._scenes["sc:1"].closed
    assert bus.join("sc:1", "a")["ok"] is False


def test_scene_bus_enforces_invited_only():
    bus = SceneBus()
    bus.create("sc:x", topic="t", actors=["a"], max_rounds=2)
    assert bus.join("sc:x", "intruder")["ok"] is False  # 未受邀拒绝（越权隔离）


def test_permission_gate_safe_allows_and_danger_blocks():
    gate = PermissionGate()  # 默认 supervised：safe->allow, danger->deny
    safe_tool = Tool("read_file", "读", "safe", lambda **kw: "data")
    danger_tool = Tool("publish", "发布", "danger", lambda **kw: "data")
    sess = SessionInfo(project_id="p1", agent="orchestrator", permission_profile="supervised")
    assert gate.check(sess, safe_tool) == "allow"
    assert gate.check(sess, danger_tool) == "deny"


def test_tool_registry_invoke_flows_levels():
    def _handler(session, params, budget=None):
        return params

    reg = ToolRegistry(gate=PermissionGate())
    reg.register(Tool("read_file", "读", "safe", _handler))
    reg.register(Tool("publish", "发布", "danger", _handler))
    sess = SessionInfo(project_id="p1", agent="orchestrator", permission_profile="supervised")
    ok_res = reg.invoke(sess, "read_file", {"x": 1})
    assert ok_res.status == "ok"
    denied_res = reg.invoke(sess, "publish", {})
    assert denied_res.status == "denied"


def test_moderation_prechecker():
    mc = ModerationPrechecker(banned=["违禁词"])
    assert mc.predicate_hit("包含违禁词内容")
    assert not mc.predicate_hit("正常内容")


def test_indexdb_upsert_and_audit(tmp_path):
    """写入必须**真的落库**（2026-09-18：此例原先零断言，写入变 no-op 也照样绿）。

    IndexDb 尚未提供查询 API，故直连 sqlite 校验两张表的行内容。
    """
    from novelist.storage.indexdb import IndexDb

    db = IndexDb(str(tmp_path / ".index.db"))
    db.init()
    db.upsert_fragment("sig:1", "plot_event", 3, 8, '["pt:V017"]', "{}")
    db.write_audit("moderation.blocked", '{"agent":"orchestrator"}', "{}")

    with db.connect() as conn:
        frag = conn.execute("SELECT sig,kind,vol,ch,refs FROM fragments").fetchall()
        audit = conn.execute("SELECT kind,session FROM audit_log").fetchall()

    assert frag == [("sig:1", "plot_event", 3, 8, '["pt:V017"]')], frag
    assert audit == [("moderation.blocked", '{"agent":"orchestrator"}')], audit

    # upsert 语义：同 sig 覆盖而非重复
    db.upsert_fragment("sig:1", "plot_event", 3, 9, "[]", "{}")
    with db.connect() as conn:
        rows = conn.execute("SELECT ch FROM fragments WHERE sig='sig:1'").fetchall()
    assert rows == [(9,)], rows
