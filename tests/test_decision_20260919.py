"""2026-09-19 决策 D-1…D-14 落地的钉死测试（逐批追加）。

本文件覆盖：
- D-4 publish/promote 合并（工具面只剩 publish，danger 级）
- D-5 obs 预算耗尽 → 工具调用被拒（不再盲跑）
"""
from __future__ import annotations

from novelist.core.session import Budget, SessionInfo
from novelist.providers.fake import FakeProvider
from novelist.tools import all_tools


# ---------- D-4：工具面只剩 publish ----------

def test_promote_draft_not_registered(ws_factory):
    """D-4：sensitive 级 promote_draft 已删——转正统一走 danger 级 publish。"""
    ws, pid = ws_factory("proj-d4")
    names = {t.name for t in all_tools(ws)}
    assert "promote_draft" not in names
    assert "publish" in names
    pub = next(t for t in all_tools(ws) if t.name == "publish")
    assert pub.level == "danger"


def test_cli_promote_uses_publish(ws_factory, monkeypatch):
    """D-4：CLI `promote` 命令转发到 publish（用户主动执行 → 当场确认档）。"""
    from click.testing import CliRunner

    from novelist import cli as cli_module

    ws, pid = ws_factory("proj-d4cli")
    ws.draft_path(pid, 1, 1).parent.mkdir(parents=True, exist_ok=True)
    ws.write_text(ws.draft_path(pid, 1, 1), "正文占位\n")

    calls: list[tuple] = []

    class _SpyRegistry:
        def invoke(self, session, name, params, budget=None):
            calls.append((name, dict(params)))
            from novelist.core.tools import ok
            return ok(data={"path": "x"})

    import novelist.tools as tools_pkg

    monkeypatch.setattr(tools_pkg, "build_registry",
                        lambda *a, **k: _SpyRegistry())
    r = CliRunner().invoke(cli_module.cli, ["promote", str(ws.root), "--vol", "1", "--ch", "1"])
    assert r.exit_code == 0, r.output
    assert calls and calls[0][0] == "publish", calls


# ---------- D-5：obs 预算耗尽 → 拒调 ----------

def test_obs_budget_exhausted_denies_tools(ws_factory):
    """D-5：观测预算用尽后工具**不再执行**，模型被明确告知收敛。"""
    from novelist.core.agent_runner import AgentRunner

    ws, pid = ws_factory("proj-d5")
    reg_calls: list[str] = []

    class _Reg:
        def invoke(self, session, name, params, budget=None):
            reg_calls.append(name)
            from novelist.core.tools import ok
            return ok(data={"big": "x" * 500})

    runner = AgentRunner(FakeProvider(), SessionInfo(project_id=pid, agent="t"),
                         budget=Budget(max_tokens_out=100, max_rounds=5),
                         registry=_Reg(), obs_total_budget_chars=50)
    runner._obs_chars = 50  # 预算已耗尽
    runner._run_tool(type("D", (), {"tool_name": "read_file", "tool_args": {},
                                    "tool_calls": None})())
    assert reg_calls == [], "预算耗尽后不得再执行工具"
    msg = runner._messages[-1]
    assert "denied" in msg.content and "不要再调用任何工具" in msg.content
    assert runner._evidence[-1]["ok"] == "obs_budget_exhausted"


def test_obs_budget_partial_still_executes(ws_factory):
    """对照：预算未耗尽时工具正常执行（不得把守卫做成恒拒）。"""
    from novelist.core.agent_runner import AgentRunner

    ws, pid = ws_factory("proj-d5b")
    reg_calls: list[str] = []

    class _Reg:
        def invoke(self, session, name, params, budget=None):
            reg_calls.append(name)
            from novelist.core.tools import ok
            return ok(data={"v": 1})

    runner = AgentRunner(FakeProvider(), SessionInfo(project_id=pid, agent="t"),
                         budget=Budget(max_tokens_out=100, max_rounds=5),
                         registry=_Reg(), obs_total_budget_chars=1000)
    runner._run_tool(type("D", (), {"tool_name": "read_file", "tool_args": {},
                                    "tool_calls": None})())
    assert reg_calls == ["read_file"]


# ---------- D-2：BIBLE_EDITABLE 与 schema 对齐（关键字段名抽查）----------

def test_bible_editable_aligned_with_schema():
    import json as _json
    from pathlib import Path as _P

    from novelist.core.bible_feedback import BIBLE_EDITABLE

    for file, spec in BIBLE_EDITABLE.items():
        sp = _P("schemas/bible") / f"{file[:-5]}.schema.json"
        sch = _json.loads(sp.read_text(encoding="utf-8"))
        props = set(((sch.get("items") or sch).get("properties") or {}))
        for fld in list(spec.get("edit") or []) + list(spec.get("readonly") or []):
            assert str(fld).split(".")[0] in props, f"{file}: {fld} 不在 schema"
    # 具体回归：漂移字段不得复活
    assert "personality" not in BIBLE_EDITABLE["characters.json"]["edit"]
    assert "core_traits" in BIBLE_EDITABLE["characters.json"]["edit"]
    assert "description" not in BIBLE_EDITABLE["plot_threads.json"]["edit"]
    assert "desc" in BIBLE_EDITABLE["plot_threads.json"]["edit"]


def test_mint_id_matches_schema_pattern():
    import re as _re

    from novelist.core.bible_feedback import mint_id

    cases = {"characters.json": "^char:[A-Za-z0-9_-]+$",
             "plot_threads.json": "^(pt|thread):[A-Za-z0-9_-]+$",
             "locations.json": "^loc:[A-Za-z0-9_-]+$",
             "items.json": "^item:",
             "skills.json": "^skill:",
             "settings.json": "^set:"}
    for file, pat in cases.items():
        # 中文名（走 hash 分支）与 ASCII 名都必须匹配
        for nm in ("林原", "YeLan"):
            new_id = mint_id(file, {"name": nm})
            assert _re.match(pat, new_id), (file, nm, new_id)
    # 去重
    existing = [{"id": mint_id("items.json", {"name": "玉牌"})}]
    again = mint_id("items.json", {"name": "玉牌"}, existing)
    assert again not in {x["id"] for x in existing}
