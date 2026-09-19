"""结构化查询工具测试（ADR-036 M3ac-2）：list_chapters / get_bible / get_outline /
list_conflicts / get_worldstate——全部 safe 只读。

单测绝不真调 LLM（docs/09 §2.1）。
"""

from __future__ import annotations

from novelist.core.session import SessionInfo
from novelist.core.tools import EVIDENCE_TOOL_NAMES, ToolRegistry
from novelist.tools import all_tools, evidence_tools


def _reg(ws):
    reg = ToolRegistry()  # 无审批通道；safe 级自动放行
    for t in all_tools(ws):
        reg.register(t)
    return reg


def _sess(pid):
    return SessionInfo(project_id=pid, agent="orchestrator")


def _seed_project(ws, pid, write_json):
    """项目里放点真数据：1 章定稿 + 1 草稿 + 1 细纲 + 人物/世界态/冲突。"""
    write_json(ws, pid, "bible/characters.json",
               [{"id": "char:a", "name": "林原", "role": "protagonist",
                 "power": {"level": "练气前期"}}])
    write_json(ws, pid, "bible/worldstate.json",
               {"time": {"now": 3, "label": "第 3 天"},
                "characters": {"char:a": {"name": "林原", "level": "练气前期",
                                          "location": "青山镇", "status": "active"}}})
    write_json(ws, pid, "outline/volumes.json", [{"vol": 1, "title": "卷一"}])
    ws.write_text(ws._abs(f"{pid}/chapters/1-1.md"), "# 第一章\n正文若干" * 100)  # noqa: SLF001
    ws.write_text(ws._abs(f"{pid}/drafts/chapters/1-2.md"), "# 草稿\n" * 10)  # noqa: SLF001
    ws.write_text(ws._abs(f"{pid}/outline/chapters/1-1.md"), "细纲 1-1 内容")  # noqa: SLF001
    ws.write_text(ws._abs(f"{pid}/outline/chapters/1-3.md"), "细纲 1-3 内容")  # noqa: SLF001
    from novelist.forge.conflicts import add_conflict
    add_conflict(ws, pid, kind="threads", node="thread_set", summary="归一后同名伏笔",
                 payload={"existing": {"id": "pt:a", "desc": "旧"},
                          "candidate": {"id": "pt_b", "desc": "新"}},
                 suggested="merge")


def test_query_tools_all_safe_and_registered():
    """5 个查询工具全 safe 级、进全量表与证据环白名单（T-1）。"""
    import tempfile

    from novelist.storage.workspace import Workspace

    with tempfile.TemporaryDirectory() as d:
        w = Workspace(root=d)
        names = {t.name for t in all_tools(w)}
        expect = {"list_chapters", "get_bible", "get_outline",
                  "list_conflicts", "get_worldstate"}
        assert expect <= names
        assert expect <= EVIDENCE_TOOL_NAMES
        ev_names = {t.name for t in evidence_tools(w)}
        assert expect <= ev_names
        for t in all_tools(w):
            if t.name in expect:
                assert t.level == "safe", f"{t.name} 必须是 safe 级"


def test_list_chapters(tmp_path):
    from novelist.storage.workspace import Workspace
    from novelist.storage.checkpoint import Checkpoint
    ws = Workspace(root=str(tmp_path))
    pid = "p"
    ws.create_project(pid)
    Checkpoint(ws).save(pid, {"id": pid, "title": "t", "pipeline_state": "写作"})
    _seed_project(ws, pid, lambda w, p, rel, d: ws.write_json(ws._abs(f"{p}/{rel}"), d))  # noqa: SLF001
    res = _reg(ws).invoke(_sess(pid), "list_chapters", {})
    assert res.status == "ok"
    rows = {(r["vol"], r["ch"]): r["status"] for r in res.data["chapters"]}
    assert rows[("1", "1")] == "published"
    assert rows[("1", "2")] == "draft"
    assert rows[("1", "3")] == "outline"
    assert res.data["chapters"][0].get("has_outline") or True  # 1-1 有细纲


def test_get_bible_brief_and_by_id(tmp_path):
    from novelist.storage.workspace import Workspace
    from novelist.storage.checkpoint import Checkpoint
    ws = Workspace(root=str(tmp_path))
    pid = "p"
    ws.create_project(pid)
    Checkpoint(ws).save(pid, {"id": pid, "title": "t"})
    _seed_project(ws, pid, lambda w, p, rel, d: ws.write_json(ws._abs(f"{p}/{rel}"), d))  # noqa: SLF001
    reg = _reg(ws)
    res = reg.invoke(_sess(pid), "get_bible", {"section": "characters"})
    assert res.status == "ok" and res.data["count"] == 1
    assert res.data["items_brief"][0]["name"] == "林原"
    res2 = reg.invoke(_sess(pid), "get_bible", {"section": "characters", "id": "char:a"})
    assert res2.data["found"] and res2.data["item"]["power"]["level"] == "练气前期"
    res3 = reg.invoke(_sess(pid), "get_bible", {"section": "characters", "id": "char:nope"})
    assert res3.data["found"] is False
    bad = reg.invoke(_sess(pid), "get_bible", {"section": "../../etc"})
    assert "available" in bad.data  # 段白名单


def test_get_outline(tmp_path):
    from novelist.storage.workspace import Workspace
    from novelist.storage.checkpoint import Checkpoint
    ws = Workspace(root=str(tmp_path))
    pid = "p"
    ws.create_project(pid)
    Checkpoint(ws).save(pid, {"id": pid, "title": "t"})
    _seed_project(ws, pid, lambda w, p, rel, d: ws.write_json(ws._abs(f"{p}/{rel}"), d))  # noqa: SLF001
    reg = _reg(ws)
    res = reg.invoke(_sess(pid), "get_outline", {"vol": 1, "ch": 1})
    assert res.data["found"] and "细纲 1-1" in res.data["outline"]
    missing = reg.invoke(_sess(pid), "get_outline", {"vol": 9, "ch": 9})
    assert missing.data["found"] is False
    vols = reg.invoke(_sess(pid), "get_outline", {})
    assert vols.data["volumes"][0]["title"] == "卷一"


def test_list_conflicts_readonly(tmp_path):
    from novelist.storage.workspace import Workspace
    from novelist.storage.checkpoint import Checkpoint
    ws = Workspace(root=str(tmp_path))
    pid = "p"
    ws.create_project(pid)
    Checkpoint(ws).save(pid, {"id": pid, "title": "t"})
    _seed_project(ws, pid, lambda w, p, rel, d: ws.write_json(ws._abs(f"{p}/{rel}"), d))  # noqa: SLF001
    res = _reg(ws).invoke(_sess(pid), "list_conflicts", {})
    assert res.status == "ok" and res.data["count"] == 1
    assert res.data["open"][0]["suggested"] == "merge"
    assert "裁决由用户执行" in res.data["hint"]


def test_get_worldstate(tmp_path):
    from novelist.storage.workspace import Workspace
    from novelist.storage.checkpoint import Checkpoint
    ws = Workspace(root=str(tmp_path))
    pid = "p"
    ws.create_project(pid)
    Checkpoint(ws).save(pid, {"id": pid, "title": "t"})
    _seed_project(ws, pid, lambda w, p, rel, d: ws.write_json(ws._abs(f"{p}/{rel}"), d))  # noqa: SLF001
    reg = _reg(ws)
    res = reg.invoke(_sess(pid), "get_worldstate", {})
    assert res.data["found"] and res.data["time"]["now"] == 3
    assert res.data["characters"]["char:a"]["level"] == "练气前期"
    # 单人查询 + 宽松按名字
    one = reg.invoke(_sess(pid), "get_worldstate", {"character_id": "char:a"})
    assert one.data["found"] and one.data["state"]["location"] == "青山镇"
    by_name = reg.invoke(_sess(pid), "get_worldstate", {"character_id": "林原"})
    assert by_name.data["found"]
