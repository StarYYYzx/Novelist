"""流水线工具 + 枢纽 Agent + 会话授权（DL-014，2026-10-24）测试。

覆盖：
- G1 工具面：provider 绑定才注册 / create_project / start_book(smoke) / build /
  approve_review / write_chapter 前置校验 / get_gaps
- G3 枢纽态：无项目对话建书、会话落根、hub 与 project_id 互斥
- G4 会话授权：a=本会话记住；danger 永不自动
- G5 FIX-1：金手指归位（不进世界观）+ meta.protagonist_hint 落盘 + scale 钳制

单测绝不真调 LLM（docs/09 §2.1）——FakeProvider / ScriptedProvider。
"""

from __future__ import annotations

import json

from novelist.core.session import SessionInfo
from novelist.core.tools import ToolRegistry
from novelist.providers.fake import FakeProvider, ScriptedProvider
from novelist.storage.workspace import Workspace
from novelist.tools import all_tools
from novelist.tools.pipeline import PipelineContext, tools as pipeline_tools


def _sess(pid):
    return SessionInfo(project_id=pid, agent="orchestrator")


def _allow_all(req):
    return "allow"


def _reg(ws, provider, ctx=None):
    """带审批通道（全允许）的注册表——sensitive 工具免交互。"""
    reg = ToolRegistry(decision_fn=_allow_all)
    for t in all_tools(ws, provider=provider, pipeline_ctx=ctx):
        reg.register(t)
    return reg


def _seed_reply(scale=None, cheat="出名就变强系统") -> str:
    return json.dumps({
        "genre": "修仙", "template_suggestion": "", "logline": "少年逆袭",
        "protagonist_hint": {"name": "林澈", "gender": "male", "cheat": cheat},
        "conflict": "宗门倾轧", "tone_hint": "热血激昂",
        "scale_hint": scale or {"volumes": 3, "chapters_per_volume": 20},
        "time_origin": "", "anchors": [], "unknowns": [],
    }, ensure_ascii=False)


def _mk_project(ws, pid="proj-tool"):
    from novelist.storage.checkpoint import Checkpoint

    ws.create_project(pid)
    Checkpoint(ws).save(pid, {"id": pid, "title": "工具测试", "pipeline_state": "立项"})
    return pid


def _load_bp(ws, pid) -> dict:
    p = ws._abs(f"{pid}/workspace/forge/blueprint.json")  # noqa: SLF001
    return json.loads(p.read_text(encoding="utf-8"))


# ---------- G1：注册纪律 ----------

def test_pipeline_tools_require_provider(tmp_path):
    """provider=None → 流水线组不注册（章节生成内部的 registry 天然拿不到它们）。"""
    ws = Workspace(root=str(tmp_path))
    names = {t.name for t in all_tools(ws)}
    assert "start_book" not in names and "write_chapter" not in names
    with_provider = {t.name for t in all_tools(ws, provider=FakeProvider())}
    assert {"create_project", "start_book", "build_blueprint", "approve_review",
            "get_gaps", "write_chapter", "revise_module"} <= with_provider


def test_create_project_safe_and_records_ctx(tmp_path):
    ws = Workspace(root=str(tmp_path))
    ctx = PipelineContext()
    reg = ToolRegistry()
    for t in pipeline_tools(ws, provider=FakeProvider(), ctx=ctx):
        reg.register(t)
    tool = reg.get("create_project")
    assert tool.level == "safe"
    res = reg.invoke(_sess(None), "create_project", {"title": "我的新书"})
    assert res.status == "ok", res.data
    pid = res.data["project_id"]
    assert pid in ws.list_projects()
    assert ctx.last_created_project == pid
    proj = json.loads(ws.project_json_path(pid).read_text(encoding="utf-8"))
    assert proj["title"] == "我的新书"


# ---------- G5 FIX-1：金手指归位 + scale 钳制 ----------

def test_start_book_smoke_cheat_goes_to_protagonist_not_worldview(tmp_path):
    """FIX-1：cheat 不再污染 worldview.mechanic，落主角卡 + meta.protagonist_hint。"""
    ws = Workspace(root=str(tmp_path))
    pid = _mk_project(ws)
    reg = _reg(ws, FakeProvider(reply=_seed_reply()))
    res = reg.invoke(_sess(pid), "start_book",
                     {"project_id": pid, "brief": "修仙少年林澈靠出名变强",
                      "smoke": True})
    assert res.status == "ok", res.data
    assert res.data["smoke"] is True
    bp = _load_bp(ws, pid)
    mechanic = (bp["worldview"].get("power_system") or {}).get("mechanic") or ""
    assert "出名就变强" not in mechanic, "金手指不得写进世界观力量机制"
    proto = [c for c in bp["characters"] if c.get("role") == "protagonist"]
    assert proto and proto[0].get("cheat") == "出名就变强系统"
    assert (bp["meta"].get("protagonist_hint") or {}).get("cheat") == "出名就变强系统"


def test_start_book_scale_clamped_when_llm_overshoots(tmp_path):
    """scale sanity：LLM 推测 6×100 → 钳回默认 3×20，且降置信让 get_gaps 拿来问。"""
    ws = Workspace(root=str(tmp_path))
    pid = _mk_project(ws)
    reg = _reg(ws, FakeProvider(reply=_seed_reply(scale={"volumes": 6,
                                                          "chapters_per_volume": 100})))
    res = reg.invoke(_sess(pid), "start_book",
                     {"project_id": pid, "brief": "一句话", "smoke": True})
    assert res.status == "ok"
    assert any("钳制" in w for w in res.data["warnings"])
    bp = _load_bp(ws, pid)
    assert bp["meta"]["scale"]["volumes"] == 3
    assert bp["meta"]["scale"]["chapters_per_volume"] == 20
    # 钳制后的规模必须是缺口（low_confidence）——对话 agent / 商讨轮都会来确认
    prov = bp.get("provenance", {}).get("meta.scale", {})
    assert prov.get("confidence", 1.0) < 0.5
    gaps = reg.invoke(_sess(pid), "get_gaps", {"project_id": pid})
    keys = {g["key"]: g["reason"] for g in gaps.data["gaps"]}
    assert keys.get("meta.scale") == "low_confidence"


def test_get_gaps_lists_required_unfilled(tmp_path):
    ws = Workspace(root=str(tmp_path))
    pid = _mk_project(ws)
    reg = _reg(ws, FakeProvider(reply=_seed_reply()))
    reg.invoke(_sess(pid), "start_book",
               {"project_id": pid, "brief": "一句话", "smoke": True})
    res = reg.invoke(_sess(pid), "get_gaps", {"project_id": pid})
    assert res.status == "ok" and res.data["count"] > 0
    labels = {g["key"] for g in res.data["gaps"]}
    assert "meta.logline" in labels or "meta.pace" in labels
    for g in res.data["gaps"]:
        assert g["question"], "缺口必须带问法（对话式填设定的原料）"


# ---------- G1：审核放行 / 构建 / 写章前置 ----------

def test_approve_review_all_and_remember(tmp_path):
    from novelist.forge.review import load_review, mark_pending, pending_modules
    from novelist.forge.state import Blueprint

    ws = Workspace(root=str(tmp_path))
    pid = _mk_project(ws)
    reg = _reg(ws, FakeProvider(reply=_seed_reply()))
    reg.invoke(_sess(pid), "start_book",
               {"project_id": pid, "brief": "一句话", "smoke": True})
    bp = Blueprint.load(ws, pid)
    mark_pending(ws, pid, bp, ["worldview", "characters"])
    assert set(pending_modules(ws, pid)) == {"worldview", "characters"}

    res = reg.invoke(_sess(pid), "approve_review",
                     {"project_id": pid, "module": "all", "remember": True})
    assert res.status == "ok", res.data
    assert sorted(res.data["approved"]) == ["characters", "worldview"]
    assert pending_modules(ws, pid) == {}
    cfg = load_review(ws, pid)
    # remember=true：开关同步永久关（与 /fr-approve-all 同语义）
    assert cfg["switches"]["worldview"] is False and cfg["switches"]["characters"] is False

    empty = reg.invoke(_sess(pid), "approve_review", {"project_id": pid, "module": "all"})
    assert empty.data["approved"] == []


def test_build_blueprint_returns_compact_summary(tmp_path):
    ws = Workspace(root=str(tmp_path))
    pid = _mk_project(ws)
    reg = _reg(ws, FakeProvider(reply="ok"))
    reg.invoke(_sess(pid), "start_book",
               {"project_id": pid, "brief": "一句话", "smoke": True})
    res = reg.invoke(_sess(pid), "build_blueprint",
                     {"project_id": pid, "resume": False, "max_calls": 2})
    assert res.status == "ok", res.data
    for key in ("ok", "gate_halted", "pending_review", "calls_used", "warnings"):
        assert key in res.data, f"构建摘要缺 {key}"
    # FakeProvider 的节点产出不可解析 → 不可能 gate_halted，但结构必须完整可读
    assert isinstance(res.data["warnings"], list)


def test_write_chapter_validates_outline_first(tmp_path):
    """无细纲 → 立刻可读失败（不烧调用）；显式给 vol/ch 同样校验。"""
    ws = Workspace(root=str(tmp_path))
    pid = _mk_project(ws)
    reg = _reg(ws, FakeProvider(reply="ok"))
    reg.invoke(_sess(pid), "start_book",
               {"project_id": pid, "brief": "一句话", "smoke": True})
    auto = reg.invoke(_sess(pid), "write_chapter", {"project_id": pid})
    assert auto.status == "error" and "细纲" in str(auto.data)
    explicit = reg.invoke(_sess(pid), "write_chapter",
                          {"project_id": pid, "vol": 1, "ch": 9})
    assert explicit.status == "error"


def test_next_chapter_deterministic(tmp_path):
    from novelist.tools.pipeline import _next_chapter

    ws = Workspace(root=str(tmp_path))
    pid = _mk_project(ws)
    for rel in ("outline/chapters/1-1.md", "outline/chapters/1-2.md",
                "outline/chapters/2-1.md", "drafts/chapters/1-1.md"):
        ws.write_text(ws._abs(f"{pid}/{rel}"), "x")  # noqa: SLF001
    ws.write_text(ws._abs(f"{pid}/chapters/1-2.md"), "x")  # noqa: SLF001
    # 1-1 草稿已写、1-2 已定稿、2-1 只有细纲 → 下一章 = 2-1
    assert _next_chapter(ws, pid) == (2, 1)


# ---------- G3：枢纽 Agent ----------

def test_hub_agent_creates_project_via_tool(tmp_path):
    """枢纽态（无项目）：ScriptedProvider 调 create_project → 项目落盘 + ctx 记录。"""
    from novelist.core.agent_chat import ChatAgent

    ws = Workspace(root=str(tmp_path))
    ctx = PipelineContext()
    script = [
        {"tool": "create_project", "args": {"title": "枢纽开的书"}},
        {"final": "已创建《枢纽开的书》，开始提炼设定。"},
    ]
    agent = ChatAgent(ws, None, ScriptedProvider(script), hub=True, pipeline_ctx=ctx)
    answer = agent.ask("帮我开一本新书")
    assert "枢纽开的书" in answer
    pid = ctx.last_created_project
    assert pid and pid in ws.list_projects()


def test_hub_agent_session_at_workspace_root(tmp_path):
    """枢纽会话落工作区根（agent_hub_session.jsonl），不进任何项目目录。"""
    from novelist.core.agent_chat import ChatAgent, HUB_SESSION_REL, load_session

    ws = Workspace(root=str(tmp_path))
    agent = ChatAgent(ws, None, FakeProvider(reply="工作台回答"), hub=True)
    agent.ask("现在有什么书")
    p = ws._abs(HUB_SESSION_REL)  # noqa: SLF001
    assert p.exists(), "枢纽会话必须落盘"
    turns = load_session(ws, None)
    assert any(t["role"] == "assistant" and "工作台回答" in t["content"] for t in turns)


def test_hub_rejects_project_binding_and_uses_hub_prompt(tmp_path):
    from novelist.core.agent_chat import HUB_SYSTEM_PROMPT, ChatAgent

    ws = Workspace(root=str(tmp_path))
    try:
        ChatAgent(ws, "proj-x", FakeProvider(), hub=True)
        raise AssertionError("hub 态绑定 project_id 必须报错")
    except ValueError:
        pass
    agent = ChatAgent(ws, None, FakeProvider(), hub=True)
    sys_msgs = [m for m in agent.runner.export_messages() if m["role"] == "system"]
    assert sys_msgs and sys_msgs[0]["content"] == HUB_SYSTEM_PROMPT


def test_project_agent_uses_v2_prompt_with_pipeline(tmp_path):
    """项目态：v2 主编剧 prompt（含工作流段落）+ 流水线工具在册。"""
    from novelist.core.agent_chat import SYSTEM_PROMPT, ChatAgent

    ws = Workspace(root=str(tmp_path))
    pid = _mk_project(ws)
    agent = ChatAgent(ws, pid, FakeProvider())
    names = {t.name for t in agent.runner.registry._tools.values()}  # noqa: SLF001
    assert "write_chapter" in names and "get_gaps" in names
    sys_msgs = [m for m in agent.runner.export_messages() if m["role"] == "system"]
    assert sys_msgs[0]["content"] == SYSTEM_PROMPT
    assert "工作流" in SYSTEM_PROMPT and "DL-014" in SYSTEM_PROMPT


# ---------- G4：会话授权 ----------

class _Req:
    """ApprovalRequest 的最小替身（_io_decision 只读 tool/params/reason）。"""

    def __init__(self, tool, params=None):
        self.tool = tool
        self.params = params or {}
        self.reason = "test"


def test_io_decision_grant_always_remembered_for_sensitive(tmp_path):
    from novelist.forge.console import FilterableIO, _io_decision

    grants: set[str] = set()
    io = FilterableIO(lines=["a", "n"], tty=False)
    decide = _io_decision(io, grants)
    assert decide(_Req("write_chapter")) == "allow"
    assert "write_chapter" in grants
    # 第二次同工具：命中 grants，直接放行（不消费输入行）
    assert decide(_Req("write_chapter")) == "allow"
    # 其它工具仍要问；本例脚本下一行是 n → 拒绝
    assert decide(_Req("start_book")) == "deny"
    assert "start_book" not in grants


def test_io_decision_never_grants_danger_tools(tmp_path):
    from novelist.forge.console import FilterableIO, _io_decision

    grants: set[str] = set()
    io = FilterableIO(lines=["a"], tty=False)
    decide = _io_decision(io, grants)
    assert decide(_Req("delete_file")) == "deny", "danger 级答 a 必须拒绝"
    assert grants == set(), "danger 级永不入会话授权"
