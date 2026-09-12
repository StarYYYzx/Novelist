"""M1 里程碑集成测试（docs/08 M1）。

用 ScriptedProvider 驱动 AgentRunner 循环：
主编剧经循环调用 write_draft 工具落盘草稿，再触发事件实时回写（ADR-013），产出本章。
覆盖：工具注册表装配、门禁、循环执行、草稿落盘、事件回写。
"""

from __future__ import annotations


from novelist.core.orchestrator import produce_chapter
from novelist.core.session import SessionInfo
from novelist.providers.fake import ScriptedProvider
from novelist.storage.checkpoint import Checkpoint
from novelist.storage.workspace import Workspace


def _new_project(tmp_path, pid="proj-m1"):
    ws = Workspace(root=str(tmp_path))
    ws.create_project(pid)
    ck = Checkpoint(ws)
    ck.save(pid, {"id": pid, "title": "demo", "pipeline_state": "立项", "event_seq": 0})
    return ws, pid


def test_agent_runner_executes_tool_and_finalizes(tmp_path):
    # 脚本：先触发 write_draft，再返回最终文本
    script = [
        {"tool": "write_draft", "args": {"vol": 1, "ch": 1, "content": "第一章草稿内容。"}},
        {"final": "本章完成"},
    ]
    provider = ScriptedProvider(script)
    ws, pid = _new_project(tmp_path)

    res = produce_chapter(ws, pid, vol=1, ch=1, provider=provider, session=SessionInfo(project_id=pid, agent="orchestrator"))

    assert res.ok is True
    assert res.result == "本章完成"
    # 草稿已落盘
    assert ws.draft_path(pid, 1, 1).exists()
    assert "第一章草稿内容" in ws.draft_path(pid, 1, 1).read_text(encoding="utf-8")
    # 事件回写触发
    assert res.events_committed == 1


def test_produce_chapter_multiple_tools(tmp_path):
    # 一个较复杂循环：write_file(试演片段) → write_draft → final
    script = [
        {"tool": "write_file", "args": {"path": "workspace/take.md", "content": "演员试演片段"}},
        {"tool": "write_draft", "args": {"vol": 2, "ch": 3, "content": "整合后正文。"}},
        {"final": "ok."},
    ]
    ws, pid = _new_project(tmp_path, "proj-m1b")
    res = produce_chapter(ws, pid, 2, 3, ScriptedProvider(script))
    assert res.ok
    # write_file 的 path 相对工作区根（session.project 由调用方在工具内对齐——此处 take 落在更上层）
    assert (tmp_path / "workspace" / "take.md").exists()
    assert ws.draft_path(pid, 2, 3).exists()


def test_loop_reaches_limit_returns_not_ok(tmp_path):
    # 脚本耗尽且不给 final -> 达到轮次上限强制收敛 -> ok=False
    script = [
        {"tool": "write_draft", "args": {"vol": 1, "ch": 1, "content": "x"}},
        {"tool": "write_draft", "args": {"vol": 1, "ch": 1, "content": "y"}},  # 始终工具调用，无 final
    ]
    ws, pid = _new_project(tmp_path, "proj-limit")
    res = produce_chapter(ws, pid, 1, 1, ScriptedProvider(script))
    assert res.ok is False  # 脚本无 final -> 循环达轮次上限收敛为失败


def test_cli_chapter_writes_draft(tmp_path):
    """CLI chapter 命令端到端：scripted provider 驱动一章草稿落盘 + 事件回写。"""
    from click.testing import CliRunner

    from novelist.cli import cli

    ws, pid = _new_project(tmp_path, "proj-cli")
    runner = CliRunner()
    # 用 --provider demo（= scripted，脚本写入草稿再 final）
    res = runner.invoke(cli, ["chapter", str(tmp_path), "--vol", "1", "--ch", "9", "--provider", "demo"])
    assert res.exit_code == 0, res.output
    assert "wrote draft" in res.output
    assert ws.draft_path(pid, 1, 9).exists()
