"""M3 细纲→先忆→正文→事件回写 全链路测试（docs/04 §4.1，ADR-011/013）。

用 ScriptedProvider 驱动，验证 CLI chapter 会：读细纲 + 前导记忆组装 goal →
经 produce_chapter 产出草稿 → 事件回写落盘 memory。不烧真实本地模型。
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from novelist.cli import cli
from novelist.storage.checkpoint import Checkpoint
from novelist.storage.workspace import Workspace


def _project(tmp_path, pid="proj-fl"):
    ws = Workspace(root=str(tmp_path))
    ws.create_project(pid)
    Checkpoint(ws).save(pid, {"id": pid, "title": "全链路", "pipeline_state": "正文", "event_seq": 0})
    # 预置细纲
    gist = ws.outline_chapter_path(pid, 1, 1)
    gist.parent.mkdir(parents=True, exist_ok=True)
    gist.write_text("细纲：主角苏晚入门派，遭遇劲敌。", encoding="utf-8")
    # 预置前情记忆（已有事件）
    ev = ws._abs(f"{pid}/memory/plot_events.json")
    ev.parent.mkdir(parents=True, exist_ok=True)
    ev.write_text(json.dumps([{"id": "ev:0", "vol": 1, "ch": 0, "summary": "上一章苏晚踏入青冥山"}]), encoding="utf-8")
    return ws, pid


def test_chapter_full_pipeline(tmp_path):
    ws, pid = _project(tmp_path)
    res = CliRunner().invoke(
        cli,
        [
            "chapter", str(tmp_path), "--provider", "scripted", "--vol", "1", "--ch", "1",
        ],
    )
    assert res.exit_code == 0, res.output
    assert "草稿已落盘" in res.output
    # 草稿落盘
    assert ws.draft_path(pid, 1, 1).exists()
    # 事件回写：memory/plot_events.json 追加了本章事件（原 1 条 + 本章）
    events = json.loads(ws._abs(f"{pid}/memory/plot_events.json").read_text(encoding="utf-8"))
    assert len(events) == 2
    assert events[-1]["summary"].startswith("完成第 1 卷第 1 章")


def test_compose_goal_injects_gist_and_memory(tmp_path):
    from novelist.cli import _compose_goal

    ws, pid = _project(tmp_path)
    goal = _compose_goal(ws, pid, 1, 1)
    assert "苏晚入门派" in goal          # 细纲注入
    assert "上一章苏晚踏入青冥山" in goal   # 前情记忆（先忆）注入
