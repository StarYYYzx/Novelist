"""Agent 层审计回归（AG-1…AG-24，docs/Agent层审计与修复方案-2026-09-15.md）。

全部离线：桩 provider / 直调 ToolRegistry，**不真调任何 LLM**。
每个用例对应审计里的一条，覆盖"以前静默出错、现在必须显式"的行为。
"""

from __future__ import annotations

import json

import pytest

from novelist.core.approval import ApprovalQueue, PersistedSessionRef
from novelist.core.llm import (
    LLMRequest,
    LLMResult,
    ModerationBlockedError,
    ToolCall,
    Usage,
)
from novelist.core.session import Budget, SessionInfo
from novelist.core.tools import (
    APPROVAL_ALLOW,
    APPROVAL_ASK,
    APPROVAL_DENY,
    SCHEMA_FAIL,
    PermissionGate,
    Tool,
    ToolRegistry,
)
from novelist.core.agent_runner import AgentLoopError, AgentRunner
from novelist.storage.checkpoint import Checkpoint
from novelist.storage.workspace import Workspace
from novelist.tools.governance import tools as gov_tools
from novelist.tools.writing import tools as write_tools


# ---------------------------------------------------------------- 夹具与桩

def _ws(tmp_path, pid="p"):
    ws = Workspace(root=str(tmp_path))
    ws.create_project(pid)
    Checkpoint(ws).save(pid, {"id": pid, "title": "长夜", "pipeline_state": "写作",
                              "event_seq": 57, "phase": "mid"})
    return ws, pid


def _sess(pid="p", profile="supervised"):
    return SessionInfo(project_id=pid, agent="orchestrator", permission_profile=profile)


class _Caps:
    def __init__(self, tool_calling=True):
        self.tool_calling = tool_calling
        self.max_context = 8000
        self.json_mode = True
        self.streaming = False
        self.embedding = False


class _StubProvider:
    """按步返回；steps 里 None = 纯文本回复，list = tool_calls。捕获每次 req。"""

    def __init__(self, steps, *, content="正文" * 200, tool_calling=True, blocked=False):
        self.steps = list(steps)
        self.reqs: list[LLMRequest] = []
        self._content = content
        self._caps = _Caps(tool_calling)
        self._blocked = blocked

    @property
    def capabilities(self):
        return self._caps

    def complete(self, req: LLMRequest) -> LLMResult:
        self.reqs.append(req)
        if self._blocked:
            return LLMResult(ok=False, blocked=True, block_reason="content_filter",
                             content="", finish_reason="content_filter", provider="stub")
        return LLMResult(ok=True, content=self._content, finish_reason="stop",
                         provider="stub", usage=Usage(tokens_in=100, tokens_out=50))


class _ToolThenTextProvider(_StubProvider):
    """首轮给 tool_calls、次轮给文本——用来验证消息协议回灌。"""

    def __init__(self, steps, *, tool="read_file", args=None, **kw):
        super().__init__(steps, **kw)
        self._tool = tool
        self._args = args if args is not None else {"path": "nope.md"}

    def complete(self, req: LLMRequest) -> LLMResult:
        self.reqs.append(req)
        if len(self.reqs) == 1:
            return LLMResult(ok=True, content="", finish_reason="tool_calls", provider="stub",
                             usage=Usage(tokens_in=100, tokens_out=20),
                             tool_calls=[ToolCall(id="call_1", name=self._tool,
                                                  arguments=self._args)])
        return LLMResult(ok=True, content="done", finish_reason="stop", provider="stub",
                         usage=Usage(tokens_in=120, tokens_out=10))


def _safe_tool(name="probe"):
    return Tool(name, "探针", "safe", lambda session, params, budget=None: {"echo": params},
                {"q": {"type": "string"}}, required=["q"])


# ---------------------------------------------------------------- AG-1 默认模式

def test_produce_chapter_defaults_to_direct(tmp_path):
    """AG-1：`produce_chapter` 默认直出（工具循环是显式开关）。"""
    from novelist.core.orchestrator import produce_chapter

    ws, pid = _ws(tmp_path)
    res = produce_chapter(ws, pid, 1, 1, _StubProvider([None]), session=_sess(pid))
    assert res.mode == "direct"
    assert res.ok, res.result
    assert ws.draft_path(pid, 1, 1).exists()


def test_tool_mode_without_draft_is_not_ok(tmp_path):
    """AG-1：provider 声称支持工具却从不回 tool_calls → 无草稿必须 `ok=False`。

    这正是真机历史表现（草稿不落盘却 ok=True + chapter_path 指向不存在的文件）。
    """
    from novelist.core.orchestrator import produce_chapter

    ws, pid = _ws(tmp_path)
    res = produce_chapter(ws, pid, 1, 1, _StubProvider([None]), session=_sess(pid),
                          prefer_direct=False)
    # 该桩只回文本（长度够）→ AgentRunner 视为 final → 工具模式无草稿
    # → 由 AG-1 兜底落盘并留痕。**不写 if/else 双分支**：两种互斥行为都判通过，
    # 实现从"落盘"翻到"判失败"时测试依旧全绿（2026-09-18 收紧）。
    assert res.ok, res.result
    assert ws.draft_path(pid, 1, 1).exists(), "ok=True 时草稿必须真实存在"
    assert any("兜底落盘" in f for f in res.soft_failures), res.soft_failures


def test_tool_mode_downgrades_without_tool_capability(tmp_path):
    """AG-20：provider 未声明 tool_calling + `--loop` → 自动降级直出并留痕。"""
    from novelist.core.orchestrator import produce_chapter

    ws, pid = _ws(tmp_path)
    res = produce_chapter(ws, pid, 1, 1, _StubProvider([None], tool_calling=False),
                          session=_sess(pid), prefer_direct=False)
    assert res.mode == "direct"
    assert res.ok, res.result
    assert any("tool_calling" in f for f in res.soft_failures), res.soft_failures


# ---------------------------------------------------------------- AG-2/AG-3/AG-17/AG-20

def test_agent_runner_sends_openai_tool_schema(tmp_path):
    """AG-2：工具定义真的下发，且是 OpenAI function 格式、**不含内部 level**。"""
    reg = ToolRegistry(gate=PermissionGate({"supervised": {"safe": "allow"}}))
    reg.register(_safe_tool())
    prov = _ToolThenTextProvider([None, None])
    runner = AgentRunner(prov, _sess(), registry=reg, max_rounds=3)
    runner.run_loop("目标")
    tools = prov.reqs[0].tools
    assert tools, "工具定义必须下发（此前 _decide 从不传 tools）"
    assert tools[0]["type"] == "function"
    fn = tools[0]["function"]
    assert fn["name"] == "probe" and fn["parameters"]["required"] == ["q"]
    assert "level" not in fn, "内部权限分级不得下发给模型"


def test_agent_runner_tool_message_protocol(tmp_path):
    """AG-3：assistant 回传 tool_calls；工具结果以 role="tool" + tool_call_id 回灌。"""
    reg = ToolRegistry(gate=PermissionGate({"supervised": {"safe": "allow"}}))
    reg.register(_safe_tool())
    prov = _ToolThenTextProvider([None, None])
    runner = AgentRunner(prov, _sess(), registry=reg, max_rounds=3)
    runner.run_loop("目标")
    msgs = prov.reqs[1].messages
    assistant = [m for m in msgs if m.role == "assistant"]
    assert assistant and assistant[-1].tool_calls and assistant[-1].tool_calls[0].id == "call_1"
    tools_msgs = [m for m in msgs if m.role == "tool"]
    assert tools_msgs and tools_msgs[-1].tool_call_id == "call_1"


def test_no_tools_when_provider_lacks_capability(tmp_path):
    """AG-20：provider 声明不支持工具调用时**不下发** tools（否则后端报错/乱回）。"""
    reg = ToolRegistry(gate=PermissionGate({"supervised": {"safe": "allow"}}))
    reg.register(_safe_tool())
    prov = _StubProvider([None], tool_calling=False)
    runner = AgentRunner(prov, _sess(), registry=reg, max_rounds=2)
    runner.run_loop("目标")
    assert prov.reqs[0].tools is None


def test_multiple_tool_calls_in_one_round_all_executed(tmp_path):
    """AG-17：一轮内多个 tool_calls 全部执行（此前只取第 0 个，其余静默丢弃）。"""
    seen: list[str] = []

    def _mk(name):
        return Tool(name, "d", "safe",
                    lambda session, params, budget=None: seen.append(name) or {"ok": name},
                    {}, required=[])

    reg = ToolRegistry(gate=PermissionGate({"supervised": {"safe": "allow"}}))
    reg.register(_mk("t1"))
    reg.register(_mk("t2"))

    class _Two(_StubProvider):
        def complete(self, req):
            self.reqs.append(req)
            if len(self.reqs) == 1:
                return LLMResult(ok=True, content="", finish_reason="tool_calls", provider="s",
                                 tool_calls=[ToolCall(id="a", name="t1", arguments={}),
                                             ToolCall(id="b", name="t2", arguments={})])
            return LLMResult(ok=True, content="done", provider="s")

    runner = AgentRunner(_Two([None, None]), _sess(), registry=reg, max_rounds=3)
    assert runner.run_loop("目标") == "done"
    assert seen == ["t1", "t2"]


# ---------------------------------------------------------------- AG-5 空内容/拦截

def test_empty_content_is_error_not_final(tmp_path):
    """AG-5：空内容 + 无 tool_calls 不再被当成"最终结果"。"""
    reg = ToolRegistry(gate=PermissionGate({"supervised": {"safe": "allow"}}))
    prov = _StubProvider([None], content="")
    runner = AgentRunner(prov, _sess(), registry=reg, max_rounds=2)
    with pytest.raises(AgentLoopError):
        runner.run_loop("目标")


def test_blocked_raises_moderation_error(tmp_path):
    """AG-5 / docs/04 §5.10：审核拦截不得静默变成空成稿。"""
    prov = _StubProvider([None], blocked=True)
    runner = AgentRunner(prov, _sess(), max_rounds=2)
    with pytest.raises(ModerationBlockedError):
        runner.run_loop("目标")


def test_no_chapter_event_when_no_text(tmp_path):
    """AG-5：无正文时不得写章级事件（防空条目污染 plot_events）。"""
    from novelist.core.orchestrator import produce_chapter

    ws, pid = _ws(tmp_path)
    # 直出但内容为空 → 生成即失败；关键断言是**不产生事件**
    res = produce_chapter(ws, pid, 1, 1, _StubProvider([None], content=""), session=_sess(pid))
    assert res.ok is False
    assert res.events_committed == 0
    assert not ws._abs(f"{pid}/memory/plot_events.json").exists()


# ---------------------------------------------------------------- AG-4 检查点不清档

def test_checkpoint_tool_preserves_project_json(tmp_path):
    """AG-4：`checkpoint` 工具不得抹掉 pipeline_state/event_seq 等字段。"""
    ws, pid = _ws(tmp_path)
    reg = ToolRegistry(gate=PermissionGate({"supervised": {"danger": "allow"}}))
    for t in gov_tools(ws):
        reg.register(t)
    before = json.loads(ws.project_json_path(pid).read_text(encoding="utf-8"))
    res = reg.invoke(_sess(pid), "checkpoint", {})
    after = json.loads(ws.project_json_path(pid).read_text(encoding="utf-8"))
    assert res.status == "ok"
    for key in ("pipeline_state", "event_seq", "phase", "title"):
        assert after.get(key) == before.get(key), f"{key} 被抹掉了：{after}"


def test_publish_does_not_reset_event_seq(tmp_path):
    """AG-4：`publish` 也不得把 event_seq 归零。"""
    ws, pid = _ws(tmp_path)
    draft = ws.draft_path(pid, 1, 1)
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("正文", encoding="utf-8")
    reg = ToolRegistry(gate=PermissionGate({"supervised": {"danger": "allow"}}))
    for t in gov_tools(ws):
        reg.register(t)
    assert reg.invoke(_sess(pid), "publish", {"vol": 1, "ch": 1}).status == "ok"
    after = json.loads(ws.project_json_path(pid).read_text(encoding="utf-8"))
    assert after["event_seq"] == 57
    assert after["pipeline_state"] == "审查"


# ---------------------------------------------------------------- AG-6/AG-7 门禁

def test_gate_fails_closed_on_unknown_decision():
    """AG-6：拼错的处置值（"alow"）必须 fail-closed，不得静默放行。"""
    gate = PermissionGate(profiles={"supervised": {"sensitive": "alow", "danger": APPROVAL_DENY}})
    reg = ToolRegistry(gate=gate)
    reg.register(_safe_tool("t_sensitive"))
    reg._tools["t_sensitive"].level = "sensitive"
    res = reg.invoke(_sess(), "t_sensitive", {"q": "x"})
    assert res.status == "denied"


def test_gate_missing_supervised_profile_does_not_crash():
    """AG-7：策略文件缺 `supervised` 段时，回退内置兜底档而不是 KeyError。

    构造要点（2026-09-19 修正）：必须让 session **请求一个不存在的档** —— 此处
    profiles 只有 `strict`，而 session 用默认 `supervised`，两次取值均 miss，
    才会走到内置兜底。原写法让 session 直接取 `strict`（该段存在），
    `check()` 第一行就命中，后两级回退一次都没执行 —— 用例名所述的场景
    根本没被构造，是**假绿**。
    """
    gate = PermissionGate(profiles={"strict": {"sensitive": APPROVAL_ALLOW,
                                               "danger": APPROVAL_DENY}})
    assert "supervised" not in gate.profiles  # 前提：默认档确实缺失

    reg = ToolRegistry(gate=gate)
    for lv in ("safe", "sensitive", "danger"):
        t = _safe_tool(f"t_{lv}")
        t.level = lv  # _safe_tool 只造 safe 级，此处改级别以走不同判定分支
        reg.register(t)

    # 回退链：strict 不命中请求档 → supervised 也缺 → 内置兜底（sensitive=ask / danger=deny）
    assert reg.invoke(_sess(), "t_safe", {"q": "x"}).status == "ok"
    # ask 在「无审批通道」的裸 registry 下降级为 deny（AG-10 口径），不崩即达标
    assert reg.invoke(_sess(), "t_sensitive", {"q": "x"}).status == "denied"
    assert reg.invoke(_sess(), "t_danger", {"q": "x"}).status == "denied"

    # 对照：命中真实存在的 strict 档时按其配置放行 sensitive（证明上面的 deny 来自兜底而非硬编码）
    assert reg.invoke(_sess(profile="strict"), "t_sensitive", {"q": "x"}).status == "ok"


def test_policy_file_rejects_invalid_decision(tmp_path):
    """AG-6：策略文件解析期就拒绝非法取值（比运行期静默放行好）。"""
    from novelist.core.errors import NovelistError

    p = tmp_path / "policy.toml"
    p.write_text('[profile.supervised]\nsensitive = "alow"\n', encoding="utf-8")
    with pytest.raises(NovelistError):
        PermissionGate.from_policy_file(str(p))


# ---------------------------------------------------------------- AG-8/AG-9 工具契约

def test_missing_required_param_returns_schema_fail():
    """AG-8：缺必填参数 → SCHEMA_FAIL 且指明缺哪个键（模型可自纠）。"""
    reg = ToolRegistry(gate=PermissionGate({"supervised": {"safe": "allow"}}))
    reg.register(_safe_tool())
    res = reg.invoke(_sess(), "probe", {})
    assert res.status == "error" and res.code == SCHEMA_FAIL
    assert "q" in res.data["error"]


def test_internal_error_carries_message():
    """AG-8：工具内部异常必须带出类型与消息（此前 data=None）。"""
    reg = ToolRegistry(gate=PermissionGate({"supervised": {"safe": "allow"}}))
    reg.register(Tool("boom", "d", "safe", lambda session, params, budget=None: 1 / 0, {}))
    res = reg.invoke(_sess(), "boom", {})
    assert res.status == "error"
    assert "ZeroDivisionError" in res.data["error"]


def test_write_draft_failure_is_not_ok(tmp_path):
    """AG-9：写类工具失败不得返回 status=ok。"""
    ws, pid = _ws(tmp_path)
    reg = ToolRegistry(gate=PermissionGate({"supervised": {"safe": "allow"}}))
    for t in write_tools(ws):
        reg.register(t)
    res = reg.invoke(_sess(pid), "write_draft", {"vol": "x", "ch": 1, "content": "y"})
    assert res.status == "error"


# ---------------------------------------------------------------- AG-10 删除防护

def test_delete_file_refuses_root(tmp_path):
    """AG-10：空路径/项目根 → 硬拒绝（`_abs("")` 是沙箱根，rmtree 会删掉整个工作区）。"""
    ws, pid = _ws(tmp_path)
    reg = ToolRegistry(gate=PermissionGate({"supervised": {"danger": "allow"}}))
    for t in gov_tools(ws):
        reg.register(t)
    for bad in ("", ".", pid):
        res = reg.invoke(_sess(pid), "delete_file", {"path": bad})
        assert res.status == "denied", bad
    assert ws.project_dir(pid).exists()


def test_delete_file_refuses_outside_allowlist(tmp_path):
    """AG-10：白名单外的路径（bible/、memory/、chapters/）一律拒绝。"""
    ws, pid = _ws(tmp_path)
    target = ws._abs(f"{pid}/bible/characters.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("[]", encoding="utf-8")
    reg = ToolRegistry(gate=PermissionGate({"supervised": {"danger": "allow"}}))
    for t in gov_tools(ws):
        reg.register(t)
    res = reg.invoke(_sess(pid), "delete_file", {"path": f"{pid}/bible/characters.json"})
    assert res.status == "denied"
    assert target.exists()


def test_delete_file_deletes_draft(tmp_path):
    """AG-10：白名单内（drafts/）正常删除。"""
    ws, pid = _ws(tmp_path)
    f = ws.draft_path(pid, 1, 1)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("草稿", encoding="utf-8")
    reg = ToolRegistry(gate=PermissionGate({"supervised": {"danger": "allow"}}))
    for t in gov_tools(ws):
        reg.register(t)
    res = reg.invoke(_sess(pid), "delete_file", {"path": f"{pid}/drafts/chapters/1-1.md"})
    assert res.status == "ok"
    assert not f.exists()


def test_default_profile_asks_for_delete_file():
    """AG-10：默认档把 delete_file 设为 ask（调用时人工确认），其余 danger 仍 deny。"""
    gate = PermissionGate()
    df = Tool("delete_file", "d", "danger", lambda **kw: None, {})
    other = Tool("publish", "d", "danger", lambda **kw: None, {})
    assert gate.check(_sess(), df) == APPROVAL_ASK
    assert gate.check(_sess(), other) == APPROVAL_DENY


# ---------------------------------------------------------------- AG-11/AG-12 观测与预算

def test_read_file_truncates_large_observation(tmp_path):
    """AG-11：read_file 单次观测有上限（此前整份文件灌进上下文）。"""
    from novelist.tools.filesys import OBS_LIMIT_CHARS, tools as fs_tools

    ws, pid = _ws(tmp_path)
    big = ws._abs(f"{pid}/drafts/big.md")
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_text("字" * (OBS_LIMIT_CHARS * 2), encoding="utf-8")
    reg = ToolRegistry(gate=PermissionGate({"supervised": {"safe": "allow"}}))
    for t in fs_tools(ws):
        reg.register(t)
    res = reg.invoke(_sess(pid), "read_file", {"path": f"{pid}/drafts/big.md"})
    assert res.data["truncated"] is True
    assert len(res.data["content"]) <= OBS_LIMIT_CHARS + 80
    assert res.data["chars"] == OBS_LIMIT_CHARS * 2


def test_get_plot_events_default_limit(tmp_path):
    """AG-11：事件流默认只回最近 N 条（此前越写越长）。"""
    from novelist.tools.memory_tools import _EVENTS_DEFAULT_LIMIT, tools as mem_tools

    ws, pid = _ws(tmp_path)
    ws.write_json(ws._abs(f"{pid}/memory/plot_events.json"),
                  [{"id": f"ev:{i}", "summary": f"s{i}"} for i in range(500)])
    reg = ToolRegistry(gate=PermissionGate({"supervised": {"safe": "allow"}}))
    for t in mem_tools(ws, embedding=None):
        reg.register(t)
    res = reg.invoke(_sess(pid), "get_plot_events", {})
    assert res.data["count"] == _EVENTS_DEFAULT_LIMIT
    assert res.data["truncated"] is True


def test_agent_runner_accounts_usage(tmp_path):
    """AG-12：循环逐轮累计 tokens/成本（此前只传单次 max_tokens_out，无累计）。"""
    budget = Budget(max_tokens_out=1000, max_rounds=3)
    prov = _StubProvider([None])
    runner = AgentRunner(prov, _sess(), budget=budget, max_rounds=3)
    runner.run_loop("目标")
    assert budget.spent_tokens_in == 100 and budget.spent_tokens_out == 50
    assert budget.spent_cost >= 0
    assert runner.usage["tokens_in"] == 100


def test_agent_runner_observation_budget(tmp_path):
    """AG-11：整轮观测总量用尽后不再回传工具输出。"""
    reg = ToolRegistry(gate=PermissionGate({"supervised": {"safe": "allow"}}))
    reg.register(Tool("big", "d", "safe",
                      lambda session, params, budget=None: {"blob": "x" * 5000}, {}))
    prov = _ToolThenTextProvider([None, None], tool="big", args={})
    runner = AgentRunner(prov, _sess(), registry=reg, max_rounds=3,
                         obs_limit_chars=100, obs_total_budget_chars=150)
    runner.run_loop("目标")
    obs = [m for m in prov.reqs[1].messages if m.role == "tool"]
    assert obs and "已截断" in obs[-1].content


# ---------------------------------------------------------------- AG-15/AG-23 审批

def test_approval_timeout_persists_and_records_history(tmp_path):
    """AG-15：超时 deny 必须落盘 + 入 history（否则别的进程还能"批准"它）。"""
    d = tmp_path / "logs"
    q = ApprovalQueue(persist_dir=str(d))
    req = q.submit("delete_file", {"path": "x"}, _sess(), "test")
    assert q.wait_for_decision(req.id, timeout=0.05) == "deny"
    persisted = json.loads((d / "pending_approvals.json").read_text(encoding="utf-8"))
    assert [p["id"] for p in persisted["pending"]] == []
    assert any(h.get("reason") == "timeout" for h in q.history())


def test_persisted_queue_uses_named_session_ref(tmp_path):
    """AG-23：跨进程恢复不再伪造 session 对象。"""
    d = tmp_path / "logs"
    q = ApprovalQueue(persist_dir=str(d))
    q.submit("delete_file", {"path": "x"}, _sess(), "test")
    q2 = ApprovalQueue.load_persisted(str(d))
    pend = q2.list_pending()
    assert pend and isinstance(pend[0].session, PersistedSessionRef)
    assert not hasattr(pend[0].session, "project_id")


# ---------------------------------------------------------------- AG-19 工具集裁剪

def test_registry_profile_selection(tmp_path):
    """AG-19：`select(profile)` 真正按权限面/证据面裁剪（此前 profile 参数被忽略）。"""
    from novelist.tools import build_registry

    ws, pid = _ws(tmp_path)
    reg = build_registry(ws)
    assert len(reg.select("safe")) > 0
    assert all(t.level == "safe" for t in reg.select("safe"))
    ev = reg.select("evidence")
    assert ev and all(t.name in {"read_file", "grep_text", "query_memory",
                                 "get_character_history", "get_plot_events"} for t in ev)
    assert {d["function"]["name"] for d in reg.to_openai_schema("safe")} == {
        t.name for t in reg.select("safe")}


# ---------------------------------------------------------------- AG-21 目标不重复

def test_goal_injected_once(tmp_path):
    """AG-21：目标只注入一次（此前每轮重复同一目标文本，白白膨胀上下文）。"""
    reg = ToolRegistry(gate=PermissionGate({"supervised": {"safe": "allow"}}))
    reg.register(_safe_tool())
    prov = _ToolThenTextProvider([None, None])
    runner = AgentRunner(prov, _sess(), registry=reg, max_rounds=3)
    runner.run_loop("独一无二的目标")
    joined = "\n".join(m.content for m in prov.reqs[-1].messages if m.role == "user")
    assert joined.count("独一无二的目标") == 1
