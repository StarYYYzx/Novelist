"""update_blueprint 写通道 + 对话态 obs 预算/播报 测试（2026-09-19 真机修复）。

背景：proj-20260919115034 真机——agent 两轮没落地改动：只会 read_file 打转
（无结构化写工具）、obs 预算跨轮泄漏（turn2 烧光 32k 后 turn3 工具全废）。
单测绝不真调 LLM。
"""

from __future__ import annotations

import json

from novelist.core.agent_runner import AgentRunner
from novelist.core.session import Budget, SessionInfo


def _proj(tmp_path, pid="p"):
    from novelist.storage.checkpoint import Checkpoint
    from novelist.storage.workspace import Workspace

    ws = Workspace(root=str(tmp_path))
    ws.create_project(pid)
    Checkpoint(ws).save(pid, {"id": pid, "title": "t", "pipeline_state": "写作"})
    return ws, pid


# ---------- update_blueprint ----------

def _reg_with_gate(ws, decision="allow"):
    from novelist.core.tools import PermissionGate, ToolRegistry
    from novelist.tools import all_tools

    reg = ToolRegistry(gate=PermissionGate({"supervised": {"sensitive": decision,
                                                           "danger": "deny"}}))
    for t in all_tools(ws):
        reg.register(t)
    return reg


def test_update_blueprint_dict_section_dotted(tmp_path):
    """dict 段点路径合并：worldview.power_system.levels 改写 + 同步 bible。"""
    ws, pid = _proj(tmp_path)
    reg = _reg_with_gate(ws)
    res = reg.invoke(SessionInfo(project_id=pid, agent="orchestrator"),
                     "update_blueprint",
                     {"section": "worldview",
                      "set": {"power_system.levels": ["练气", "筑基"], "name": "九霄"}})
    assert res.status == "ok", res.data
    assert "worldview.power_system.levels" in res.data["changed"]
    import json as _j

    bp = _j.loads(ws._abs(f"{pid}/workspace/forge/blueprint.json").read_text("utf-8"))  # noqa: SLF001
    assert bp["worldview"]["power_system"]["levels"] == ["练气", "筑基"]
    wv = _j.loads(ws.bible_path(pid, "worldview").read_text("utf-8"))
    assert wv["name"] == "九霄"  # sync_bible 已同步


def test_update_blueprint_list_section_upsert(tmp_path):
    """list 段按 id 合并条目；不存在则新建。"""
    ws, pid = _proj(tmp_path)
    reg = _reg_with_gate(ws)
    res = reg.invoke(SessionInfo(project_id=pid, agent="orchestrator"),
                     "update_blueprint",
                     {"section": "characters", "id": "char:linyuan",
                      "set": {"name": "林原", "power.level": "筑基前期"}})
    assert res.status == "ok"
    assert any("新建条目" in c for c in res.data["changed"])
    bp = json.loads(ws._abs(f"{pid}/workspace/forge/blueprint.json").read_text("utf-8"))  # noqa: SLF001
    row = next(c for c in bp["characters"] if c["id"] == "char:linyuan")
    assert row["power.level"] == "筑基前期"  # list 段字段平铺（不按点路径拆）


def test_update_blueprint_denied_by_gate(tmp_path):
    """sensitive 级：策略 deny 时写不进去（门禁生效）。"""
    ws, pid = _proj(tmp_path)
    reg = _reg_with_gate(ws, decision="deny")
    res = reg.invoke(SessionInfo(project_id=pid, agent="orchestrator"),
                     "update_blueprint", {"section": "meta", "set": {"title": "x"}})
    assert res.status == "denied"


def test_update_blueprint_bad_section_and_bad_value(tmp_path):
    ws, pid = _proj(tmp_path)
    reg = _reg_with_gate(ws)
    sess = SessionInfo(project_id=pid, agent="orchestrator")
    assert reg.invoke(sess, "update_blueprint",
                      {"section": "../../etc", "set": {"a": 1}}).status == "error"
    assert reg.invoke(sess, "update_blueprint",
                      {"section": "meta", "set": "notadict"}).status == "error"


# ---------- 对话态 obs 预算（跨轮泄漏修复） ----------

class _LoopyReader:
    """每轮都调 read_file 的 provider 桩（最终给文本）。"""

    capabilities = type("C", (), {"tool_calling": True, "max_context": 8000,
                                  "json_mode": True, "streaming": False,
                                  "embedding": False})()

    def __init__(self, turns: int):
        self.turns = turns

    def complete(self, req):
        from novelist.core.llm import LLMResult, ToolCall, Usage

        self.turns -= 1
        if self.turns > 0:
            return LLMResult(ok=True, content="", finish_reason="tool_calls",
                             tool_calls=[ToolCall(id=f"c{self.turns}", name="read_file",
                                                  arguments={"path": "project.json"})],
                             usage=Usage(tokens_in=1, tokens_out=1))
        return LLMResult(ok=True, content="答完了", finish_reason="stop",
                         usage=Usage(tokens_in=1, tokens_out=1))


def test_obs_budget_resets_per_chat_turn(tmp_path):
    """真机 bug（proj-20260919115034）：obs 预算跨轮累计 → turn2 烧光 32k 后
    turn3「工具已不可再调用」。现在每轮 run_chat 开始归零。

    断言手法：同一 runner 连跑三轮同样的"读两次再答"，每轮结束 _obs_chars 应相等
    （未重置时第二轮起会翻倍并触发「观测预算已用尽」顶替）。
    """
    ws, pid = _proj(tmp_path)
    from novelist.core.tools import PermissionGate, ToolRegistry
    from novelist.tools import all_tools

    reg = ToolRegistry(gate=PermissionGate({"supervised": {"safe": "allow"}}))
    for t in all_tools(ws):
        reg.register(t)
    runner = AgentRunner(_LoopyReader(3), SessionInfo(project_id=pid, agent="t"),
                         budget=Budget(max_tokens_out=100, max_rounds=10),
                         registry=reg, obs_limit_chars=50, obs_total_budget_chars=100)
    r1 = runner.run_chat("第一轮")
    assert r1.final == "答完了"
    spent_1 = runner._obs_chars  # noqa: SLF001  （单次观测可略超总预算， capped 语义在 _observe）
    assert spent_1 > 0

    runner.provider = _LoopyReader(3)  # 重 arm 同样的三轮行为
    r2 = runner.run_chat("第二轮")
    assert r2.final == "答完了"
    assert runner._obs_chars == spent_1, (  # noqa: SLF001
        f"第二轮 obs 消耗应与第一轮相同（各自从 0 起算），实际 {runner._obs_chars} vs {spent_1}")  # noqa: SLF001

    # 第三轮前人为打爆计数器——run_chat 仍必须正常工作（归零在轮首）
    runner._obs_chars = 99999  # noqa: SLF001
    runner.provider = _LoopyReader(3)
    r3 = runner.run_chat("第三轮")
    assert r3.final == "答完了"
    assert runner._obs_chars == spent_1  # noqa: SLF001


def test_trace_tools_emits_call_lines(tmp_path, capsys):
    """trace_tools=True：每次工具调用打一行「→ 调用 xxx(...)」（编码 agent 式可见性）。"""
    ws, pid = _proj(tmp_path)
    from novelist.core.tools import PermissionGate, ToolRegistry
    from novelist.tools import all_tools

    reg = ToolRegistry(gate=PermissionGate({"supervised": {"safe": "allow"}}))
    for t in all_tools(ws):
        reg.register(t)
    runner = AgentRunner(_LoopyReader(2), SessionInfo(project_id=pid, agent="t"),
                         budget=Budget(max_tokens_out=100), registry=reg)
    runner.trace_tools = True
    runner.run_chat("读一下")
    out = capsys.readouterr().out
    assert "→ 调用 read_file(" in out
