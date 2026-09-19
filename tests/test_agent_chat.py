"""常驻对话 Agent（ADR-036 / M3ac）测试：ChatAgent + 会话持久化 + console 通道。

单测绝不真调 LLM（docs/09 §2.1）——FakeProvider/ScriptedProvider。
"""

from __future__ import annotations

import json

from novelist.core.agent_chat import (ChatAgent, DEFAULT_MAX_COST, SYSTEM_PROMPT,
                                      append_session, load_session,
                                      project_snapshot)
from novelist.providers.fake import FakeProvider, ScriptedProvider


def _proj(ws_factory, pid="proj-chat"):
    ws, pid = ws_factory(pid)
    return ws, pid


# ---------- system prompt 与快照（M3ac-4 缓存纪律）----------

def test_system_prompt_is_static_rules_only():
    """缓存纪律：system prompt 只放静态规则，不得放逐轮变化的内容（项目名/章节数等）。"""
    assert "主编剧" in SYSTEM_PROMPT and "边界" in SYSTEM_PROMPT
    # 不含任何 format 占位或项目特定内容——它是所有项目共享的静态前缀
    assert "{project" not in SYSTEM_PROMPT and "{title" not in SYSTEM_PROMPT
    assert "{name" not in SYSTEM_PROMPT and "proj-" not in SYSTEM_PROMPT


def test_project_snapshot_compact(ws_factory):
    ws, pid = _proj(ws_factory)
    snap = project_snapshot(ws, pid)
    assert pid in snap and "测试书名" in snap
    assert "只在对话开始时给一次" in snap


# ---------- 会话持久化（M3ac-3）----------

def test_session_roundtrip_and_budget(ws_factory):
    ws, pid = _proj(ws_factory)
    append_session(ws, pid, {"type": "turn", "role": "user", "content": "第一句"})
    append_session(ws, pid, {"type": "turn", "role": "assistant", "content": "回答一"})
    append_session(ws, pid, {"type": "usage", "turn": 1, "tokens_in": 1,
                             "tokens_out": 2, "cost": 0.001})
    assert len(load_session(ws, pid)) == 2
    # usage 记录不进对话回放
    assert all(t["role"] in ("user", "assistant") for t in load_session(ws, pid))
    # 预算截断：从尾部取最近窗口
    for i in range(20):
        append_session(ws, pid, {"type": "turn", "role": "user",
                                 "content": f"第{i}轮 " + "长" * 400})
    tail = load_session(ws, pid, budget_chars=2000)
    assert tail and tail[-1]["content"].startswith("第19轮")
    assert sum(len(t["content"]) for t in tail) <= 2000 + 500  # 单条可超，取到即停


def test_session_per_project_isolation(ws_factory):
    """按项目分文件：两本书的会话天然隔离（无需段标记）。"""
    ws, pid = _proj(ws_factory, "proj-a")
    append_session(ws, pid, {"type": "turn", "role": "user", "content": "甲书的话"})
    ws2, pid2 = _proj(ws_factory, "proj-b")
    assert load_session(ws2, pid2) == []
    assert load_session(ws, pid)[0]["content"] == "甲书的话"


# ---------- ChatAgent 对话循环 ----------

def test_chat_agent_answers_and_persists(ws_factory):
    """FakeProvider 直出：回答进 session.jsonl，usage 记账。"""
    ws, pid = _proj(ws_factory)
    agent = ChatAgent(ws, pid, FakeProvider(reply="这是主编剧的回答。"),
                      decision_fn=lambda req: "deny")
    out = agent.ask("这本书写到哪了？")
    assert out == "这是主编剧的回答。"
    turns = load_session(ws, pid)
    assert [t["role"] for t in turns] == ["user", "assistant"]
    st = agent.status()
    assert st["turns"] == 1 and st["max_cost"] == DEFAULT_MAX_COST
    assert "¥" in agent.status_line()
    # 第二轮：runner 消息流含快照 + 历史 + 新问（前缀稳定增长）
    agent.ask("再说细点")
    assert agent.status()["turns"] == 2


def test_chat_agent_uses_tools_then_answers(ws_factory):
    """ScriptedProvider：先调只读工具取证，再回答——FC 真接线在对话态的验收。"""
    ws, pid = _proj(ws_factory)
    ws.write_json(ws._abs(f"{pid}/bible/characters.json"),  # noqa: SLF001
                  [{"id": "char:a", "name": "林原"}])
    script = [
        {"tool": "get_bible", "args": {"section": "characters"}},
        {"final": "查过了：bible/characters.json 里只有 char:a（林原）。"},
    ]
    agent = ChatAgent(ws, pid, ScriptedProvider(script),
                      decision_fn=lambda req: "deny")
    out = agent.ask("书里有哪些角色？")
    assert "林原" in out
    ev = agent.runner.evidence
    assert any(e.get("kind") == "tool" and e.get("tool") == "get_bible" for e in ev)


def test_chat_agent_round_limit_converges(ws_factory):
    """轮次耗尽 → converge 抢救（AG-13 同语义），不抛给用户。"""
    ws, pid = _proj(ws_factory)
    # 3 轮全调工具 → 撞上限；converge 调用吃到第 4 条（纯文本抢救回答）
    script = [{"tool": "list_chapters", "args": {}}] * 3 + [{"final": "抢救回答"}]
    agent = ChatAgent(ws, pid, ScriptedProvider(script),
                      decision_fn=lambda req: "deny", max_rounds=3)
    out = agent.ask("一直读一直读")
    assert out == "抢救回答"


def test_chat_agent_history_replay_on_reopen(ws_factory):
    """重开（新实例）自动续接：历史轮次进消息流，快照重新生成。"""
    ws, pid = _proj(ws_factory)
    a1 = ChatAgent(ws, pid, FakeProvider(reply="第一轮回答"))
    a1.ask("记住：主角叫林原")
    a2 = ChatAgent(ws, pid, FakeProvider(reply="第二轮"))
    msgs = a2.runner.export_messages()
    # msgs[0] 是主编剧 SYSTEM_PROMPT（2026-09-19 修复：回放不再抹掉 system）；
    # msgs[1] 是新快照；历史里含上一轮的 user/assistant
    assert msgs[0]["role"] == "system" and "主编剧" in msgs[0]["content"]
    assert "【当前项目】" in msgs[1]["content"]
    assert any(m["content"] == "记住：主角叫林原" for m in msgs)
    assert any(m["content"] == "第一轮回答" for m in msgs)


def test_system_prompt_survives_history_replay(ws_factory):
    """回归钉死：load_messages 不得抹掉先注入的 system prompt（审计 P0）。"""
    ws, pid = _proj(ws_factory)
    agent = ChatAgent(ws, pid, FakeProvider(reply="ok"))
    agent.ask("你好")
    roles = [m["role"] for m in agent.runner.export_messages()]
    assert roles[0] == "system" and roles.count("system") == 1


def test_chat_agent_cost_cap_enforced(ws_factory):
    """S-5：enforce_cost=True + max_cost 硬顶——超成本即 AgentLoopError 被抢救接住。"""
    ws, pid = _proj(ws_factory)
    agent = ChatAgent(ws, pid, FakeProvider(reply="回答"),
                      decision_fn=lambda req: "deny", max_cost=0.0)
    out = agent.ask("烧钱吗")
    assert isinstance(out, str)  # 不炸 REPL；抢救路径给出回答


# ---------- console 通道（M3ac-1）----------

def test_console_natural_language_goes_to_chat(tmp_path):
    """console：非 / 输入进对话 agent（FakeProvider 直出），回答打到 io。"""
    from novelist.forge.console import Console, FilterableIO
    from novelist.storage.checkpoint import Checkpoint
    from novelist.storage.workspace import Workspace

    ws = Workspace(root=str(tmp_path))
    ws.create_project("proj-x")
    Checkpoint(ws).save("proj-x", {"id": "proj-x", "title": "测试书",
                                   "pipeline_state": "写作"})
    io = FilterableIO(lines=["书里有什么角色？", "/agent", "/q"], tty=True)
    c = Console(io=io, workspace_root=str(tmp_path), provider="fake")
    c.state.project_id = "proj-x"
    c.run()
    joined = "\n".join(io.out)
    assert "演示：fake provider 直接返回文本。" in joined  # agent 的回答
    assert "[agent]" in joined and "轮" in joined  # /agent status


def test_console_chat_tool_call_with_approval_prompt(tmp_path):
    """对话中 agent 调 sensitive 工具 → console 当场审批（deny 则工具被拒、agent 继续答）。"""
    from novelist.forge.console import Console, FilterableIO
    from novelist.storage.checkpoint import Checkpoint
    from novelist.storage.workspace import Workspace
    from novelist.core.agent_chat import ChatAgent

    ws = Workspace(root=str(tmp_path))
    ws.create_project("proj-y")
    Checkpoint(ws).save("proj-y", {"id": "proj-y", "title": "t", "pipeline_state": "写作"})
    # 预置一个 ScriptedProvider 的 agent：调 write_file（受控路径 sensitive）→ 被拒 → 兜底回答
    script = [
        {"tool": "write_file", "args": {"path": "project.json", "content": "x"}},
        {"final": "写 project.json 需要你批准；你拒绝了，我没写。"},
    ]
    agent = ChatAgent(ws, "proj-y", ScriptedProvider(script),
                      decision_fn=None)  # 由 console 包 _io_decision
    # 输入顺序：用户问话 → 审批提问答 n → 退出
    io = FilterableIO(lines=["把 project.json 改掉", "n", "/q"], tty=True)
    c = Console(io=io, workspace_root=str(tmp_path), provider="fake")
    c.state.project_id = "proj-y"
    # 预注 agent（测试注入点）：decision_fn 换成 console 的 io 通道
    from novelist.forge.console import _io_decision

    agent.runner.registry._decision_fn = _io_decision(io)  # noqa: SLF001
    c._chat_agents["proj-y"] = agent
    c.run()
    joined = "\n".join(io.out)
    assert "[approval]" in joined  # 审批提问出现
    assert "没写" in joined
    # project.json 未被改写
    assert json.loads((tmp_path / "proj-y" / "project.json").read_text(encoding="utf-8"))["title"] == "t"
