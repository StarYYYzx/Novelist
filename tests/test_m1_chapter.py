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

    res = produce_chapter(ws, pid, vol=1, ch=1, provider=provider,
                          session=SessionInfo(project_id=pid, agent="orchestrator"),
                          prefer_direct=False)  # AG-1：工具循环现在是显式开关

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
    res = produce_chapter(ws, pid, 2, 3, ScriptedProvider(script), prefer_direct=False)
    assert res.ok
    # write_file 的 path 相对工作区根（session.project 由调用方在工具内对齐——此处 take 落在更上层）
    assert (tmp_path / "workspace" / "take.md").exists()
    assert ws.draft_path(pid, 2, 3).exists()


def test_loop_reaches_limit_keeps_landed_draft(tmp_path):
    """AG-13（拍板 A「条件收敛」）：轮次耗尽但**草稿已落盘** → 以草稿为准成功收尾 + 留痕。

    旧行为是直接抛 AgentLoopError → 整章判失败，把已经写好的草稿一起否掉；
    新语义：草稿在 → ok=True（并记 soft_failure 说明"循环未正常收尾"）；
    草稿不在 → 追加一次强制收敛调用抢救内容。
    """
    script = [
        {"tool": "write_draft", "args": {"vol": 1, "ch": 1, "content": "第一章草稿内容。"}},
        {"tool": "write_draft", "args": {"vol": 1, "ch": 1, "content": "第二章版本草稿。"}},
    ]
    ws, pid = _new_project(tmp_path, "proj-limit")
    res = produce_chapter(ws, pid, 1, 1, ScriptedProvider(script), prefer_direct=False,
                          max_rounds=2)
    assert res.ok is True                      # 草稿已落盘 → 不再把整章判失败
    assert ws.draft_path(pid, 1, 1).exists()
    assert any("未正常收尾" in f for f in res.soft_failures), res.soft_failures


def test_loop_reaches_limit_without_draft_fails(tmp_path):
    """轮次耗尽且**无草稿**、收敛也拿不到有效内容 → 显式失败（不得 ok=True 无产物）。"""
    script = [
        {"tool": "read_file", "args": {"path": "nope.md"}},   # 循环内：只有工具调用
        {"tool": "read_file", "args": {"path": "nope.md"}},
        {"tool": "read_file", "args": {"path": "nope.md"}},   # 收敛调用：仍只给工具调用（无正文）
    ]
    ws, pid = _new_project(tmp_path, "proj-limit2")
    res = produce_chapter(ws, pid, 1, 1, ScriptedProvider(script), prefer_direct=False,
                          max_rounds=2)
    assert res.ok is False
    assert not ws.draft_path(pid, 1, 1).exists()


def test_cli_chapter_writes_draft(tmp_path):
    """CLI chapter 命令端到端：scripted provider 驱动一章草稿落盘 + 事件回写。"""
    from click.testing import CliRunner

    from novelist.cli import cli

    ws, pid = _new_project(tmp_path, "proj-cli")
    runner = CliRunner()
    # 用 --provider demo（= scripted，脚本先调 write_draft 再给 final）→ 必须显式 --loop：
    # 默认已是直出（AG-1/AG-18），scripted 的"工具脚本"只在工具模式下有意义。
    res = runner.invoke(cli, ["chapter", str(tmp_path), "--vol", "1", "--ch", "9",
                              "--provider", "demo", "--loop"])
    assert res.exit_code == 0, res.output
    assert "草稿已落盘" in res.output
    assert "用时" in res.output  # U6：长任务收尾须报总耗时
    assert ws.draft_path(pid, 1, 9).exists()
