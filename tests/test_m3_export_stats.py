"""M3 导出/统计/治理工具测试（docs/02 F8，docs/05 §4）。"""

import json

from click.testing import CliRunner

from novelist.cli import cli
from novelist.core.export import collect_stats, export_project
from novelist.core.session import SessionInfo
from novelist.storage.checkpoint import Checkpoint
from novelist.storage.workspace import Workspace
from novelist.tools import build_registry


def _project(tmp_path, pid="proj-x"):
    ws = Workspace(root=str(tmp_path))
    ws.create_project(pid)
    Checkpoint(ws).save(pid, {"id": pid, "title": "demo", "pipeline_state": "正文", "event_seq": 0})
    return ws, pid


# ---------- export ----------


def test_export_project_concatenates_published(tmp_path):
    ws, pid = _project(tmp_path)
    ch = ws.chapter_path(pid, 1, 1)
    ch.parent.mkdir(parents=True, exist_ok=True)
    ch.write_text("第一章正文。", encoding="utf-8")
    text = export_project(ws, pid)
    assert "第一章正文" in text
    assert "1-1" in text  # 章节文件名


def test_export_include_drafts(tmp_path):
    ws, pid = _project(tmp_path)
    d = ws.draft_path(pid, 1, 2)
    d.parent.mkdir(parents=True, exist_ok=True)
    d.write_text("草稿内容", encoding="utf-8")
    text = export_project(ws, pid, include_drafts=True)
    assert "草稿内容" in text
    assert "草稿" in text


# ---------- stats ----------


def test_collect_stats_counts(tmp_path):
    ws, pid = _project(tmp_path)
    (ws.chapter_path(pid, 1, 1)).parent.mkdir(parents=True, exist_ok=True)
    (ws.chapter_path(pid, 1, 1)).write_text("一二三四五六", encoding="utf-8")  # 6 字
    # plot_events
    ev = ws._abs(f"{pid}/memory/plot_events.json")
    ev.parent.mkdir(parents=True, exist_ok=True)
    ev.write_text(json.dumps([{"id": "e1"}, {"id": "e2"}]), encoding="utf-8")
    s = collect_stats(ws, pid)
    assert s.chapters == 1
    assert s.total_words == 6
    assert s.plot_events == 2


# ---------- governance tools（danger 门禁） ----------


def test_governance_publish_denied_by_default(tmp_path):
    ws, pid = _project(tmp_path)
    reg = build_registry(ws)  # 默认 supervised：danger=deny
    sess = SessionInfo(project_id=pid, agent="orchestrator")
    res = reg.invoke(sess, "publish", {"vol": 1, "ch": 1})
    assert res.status == "denied"


def test_governance_publish_allowed_with_ask_approved(tmp_path):
    from novelist.core.approval import ApprovalQueue
    from novelist.core.tools import APPROVAL_ASK, PermissionGate

    ws, pid = _project(tmp_path)
    d = ws.draft_path(pid, 1, 1)
    d.parent.mkdir(parents=True, exist_ok=True)
    d.write_text("已批准草稿", encoding="utf-8")
    gate = PermissionGate(profiles={"supervised": {"sensitive": APPROVAL_ASK, "danger": APPROVAL_ASK, "tools": {}}})

    def _auto_allow(req):
        return "allow"

    reg = build_registry(ws, gate=gate, approvals=ApprovalQueue(), decision_fn=_auto_allow)
    sess = SessionInfo(project_id=pid, agent="orchestrator")
    res = reg.invoke(sess, "publish", {"vol": 1, "ch": 1})
    assert res.status == "ok"
    assert ws.chapter_path(pid, 1, 1).exists()


# ---------- CLI export/stats ----------


def test_cli_export_and_stats(tmp_path):
    ws, pid = _project(tmp_path)
    ch = ws.chapter_path(pid, 1, 1)
    ch.parent.mkdir(parents=True, exist_ok=True)
    ch.write_text("正文内容。", encoding="utf-8")

    r = CliRunner().invoke(cli, ["stats", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "chapters(published): 1" in r.output

    r2 = CliRunner().invoke(cli, ["export", str(tmp_path), "--output", str(tmp_path / "out.md")])
    assert r2.exit_code == 0, r2.output
    out = tmp_path / "out.md"
    assert out.exists()
    assert "正文内容。" in out.read_text(encoding="utf-8")


# ---------- export → docx（M3o 接线） ----------


def test_cli_export_docx_roundtrip(tmp_path):
    """export --format docx 产出 Word 成稿，读回保真章题。"""
    from novelist.core.docxconv import docx_to_markdown

    ws, pid = _project(tmp_path)
    ch = ws.chapter_path(pid, 1, 1)
    ch.parent.mkdir(parents=True, exist_ok=True)
    ch.write_text("# 第 1 章 初入宗门\n\n叶蓝睁开眼。\n", encoding="utf-8")

    out = tmp_path / "book.docx"
    r = CliRunner().invoke(cli, ["export", str(tmp_path),
                                 "--format", "docx", "--output", str(out)])
    assert r.exit_code == 0, r.output
    assert out.exists()
    md = docx_to_markdown(out)
    assert "第 1 章 初入宗门" in md
    assert "叶蓝睁开眼。" in md


def test_cli_export_docx_requires_output(tmp_path):
    _project(tmp_path)
    r = CliRunner().invoke(cli, ["export", str(tmp_path), "--format", "docx"])
    assert r.exit_code != 0
    assert "--output" in r.output
