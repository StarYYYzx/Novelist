"""批次 A 回归：落盘原子性 + 工具权限边界（docs/现状审查与下一步规划-2026-09-18.md §2）。

全部离线，直调 `ToolRegistry` / `Workspace`，**不真调 LLM**。每条对应一个"会造成不可逆损失
或绕过门禁"的缺陷，覆盖修复后的应然行为。
"""

from __future__ import annotations

import os

import pytest

from novelist.core.errors import DENIED, INTERNAL
from novelist.core.session import SessionInfo
from novelist.core.tools import (
    APPROVAL_ASK,
    PermissionGate,
    ToolRegistry,
)
from novelist.storage.checkpoint import Checkpoint
from novelist.storage.workspace import Workspace
from novelist.tools.filesys import tools as filesys_tools
from novelist.tools.governance import tools as gov_tools


def _ws(tmp_path, pid="p"):
    ws = Workspace(root=str(tmp_path))
    ws.create_project(pid)
    Checkpoint(ws).save(pid, {"id": pid, "title": "长夜", "pipeline_state": "写作",
                              "event_seq": 57, "phase": "mid"})
    return ws, pid


def _reg(ws, *, danger="deny"):
    """默认档：sensitive=ask（无通道即 deny）、danger 可按需放行（publish 是 danger）。"""
    gate = PermissionGate(profiles={"supervised": {
        "sensitive": APPROVAL_ASK, "danger": danger, "tools": {"delete_file": APPROVAL_ASK}}})
    reg = ToolRegistry(gate=gate, decision_fn=lambda req: "allow")
    for t in filesys_tools(ws):
        reg.register(t)
    for t in gov_tools(ws):
        reg.register(t)
    return reg


def _sess(pid="p"):
    return SessionInfo(project_id=pid, agent="orchestrator")


# ------------------------------------------------------- A-1 正文落盘原子性

def test_write_text_is_atomic_replace(tmp_path):
    """P0-1：`write_text` 必须 tmp+rename——中断不得留下半截正文。"""
    ws, pid = _ws(tmp_path)
    target = ws.draft_path(pid, 1, 1)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("旧正文" * 100, encoding="utf-8")

    import novelist.storage.workspace as ws_mod

    real_replace = os.replace
    calls = {"n": 0}

    def _boom(a, b):
        calls["n"] += 1
        raise OSError("disk full")

    ws_mod.os.replace = _boom  # type: ignore[attr-defined]
    try:
        with pytest.raises(OSError):
            ws.write_text(target, "新正文被截断")
    finally:
        ws_mod.os.replace = real_replace  # type: ignore[attr-defined]

    assert calls["n"] == 1, "必须走 os.replace（原子换名）"
    # 旧内容分毫未动
    assert target.read_text(encoding="utf-8") == "旧正文" * 100
    # 半成品留在 .tmp，不污染正式文件
    assert target.with_name(target.name + ".tmp").exists()


def test_write_text_leaves_no_tmp_on_success(tmp_path):
    ws, pid = _ws(tmp_path)
    p = ws.draft_path(pid, 1, 2)
    ws.write_text(p, "正文内容")
    assert p.read_text(encoding="utf-8") == "正文内容"
    assert not list(p.parent.glob("*.tmp"))


# ------------------------------------------------------- A-2 write_file 权限

def test_write_file_cross_project_is_denied(tmp_path):
    """P0-2：跨项目写硬拒（此前 safe 级自动放行，可改别的项目）。"""
    ws, pid = _ws(tmp_path, pid="proj-a")
    ws.create_project("proj-b")
    Checkpoint(ws).save("proj-b", {"id": "proj-b"})
    (tmp_path / "proj-b" / "chapters").mkdir(parents=True, exist_ok=True)
    reg = _reg(ws, danger="allow")
    res = reg.invoke(_sess(pid), "write_file",
                     {"path": "proj-b/chapters/1-1.md", "content": "越权写入"})
    assert res.status == "denied" and res.code == DENIED
    assert not (tmp_path / "proj-b" / "chapters" / "1-1.md").exists()
    assert not (tmp_path / "proj-a" / "proj-b").exists(), "也不得在当前项目里造出嵌套目录"


def test_write_file_draft_area_is_auto_allowed(tmp_path):
    """8-1 拍板：草稿区/围读产物仍自动放行，不因收紧而堵死 Agent 本职动作。"""
    ws, pid = _ws(tmp_path)
    reg = _reg(ws)
    res = reg.invoke(_sess(pid), "write_file",
                     {"path": "drafts/note.md", "content": "取材笔记"})
    assert res.status == "ok", res.data
    assert (tmp_path / pid / "drafts" / "note.md").read_text(encoding="utf-8") == "取材笔记"


def test_write_file_controlled_path_requires_approval(tmp_path):
    """8-1 拍板：chapters/ 等受控位置升为 sensitive → 无审批通道即拒绝（不再静默放写）。"""
    ws, pid = _ws(tmp_path)
    # 无审批通道（默认档 sensitive=ask）→ 受控位置应当被拒，而不是静默落盘
    reg = ToolRegistry()
    for t in filesys_tools(ws):
        reg.register(t)
    res = reg.invoke(_sess(pid), "write_file",
                     {"path": "chapters/1-1.md", "content": "绕过 publish 直接转正"})
    assert res.status == "denied", res.data
    assert not (tmp_path / pid / "chapters" / "1-1.md").exists()


# ------------------------------------------------------- A-3 delete_file 基准

def test_delete_file_bare_rel_deletes_inside_project(tmp_path):
    """P0-3：按工具描述传 `drafts/x.md` 必须落到**当前项目**内（此前落到沙箱根 → NOT_FOUND）。"""
    ws, pid = _ws(tmp_path)
    f = tmp_path / pid / "drafts" / "1-9.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("废弃草稿", encoding="utf-8")
    reg = _reg(ws)  # delete_file 默认档 ask：_reg 已挂放行回调
    res = reg.invoke(_sess(pid), "delete_file", {"path": "drafts/1-9.md"})
    assert res.status == "ok", res.data
    assert not f.exists()


def test_delete_file_cannot_reach_other_project(tmp_path):
    """P0-3：带别的项目名前缀时拒绝（此前白名单按剥前缀后的路径成立 → 可跨项目删）。"""
    ws, pid = _ws(tmp_path, pid="proj-a")
    ws.create_project("proj-b")
    victim = tmp_path / "proj-b" / "drafts" / "keep.md"
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_text("别项目的草稿", encoding="utf-8")

    reg = _reg(ws, danger="allow")
    res = reg.invoke(_sess(pid), "delete_file", {"path": "proj-b/drafts/keep.md"})
    assert res.status == "denied" and res.code == DENIED
    assert victim.exists()


# ------------------------------------------------------- A-4 project.json 保护

def test_publish_does_not_wipe_project_json_on_corrupt(tmp_path):
    """P0-5：project.json 读失败时**拒绝覆写**（此前 except→{} 会把 event_seq/title 洗掉）。"""
    ws, pid = _ws(tmp_path)
    pj = ws.project_json_path(pid)
    pj.write_text("{ 这不是合法 json", encoding="utf-8")

    reg = _reg(ws, danger="allow")
    draft = ws.draft_path(pid, 1, 1)
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("正文", encoding="utf-8")

    res = reg.invoke(_sess(pid), "publish", {"vol": 1, "ch": 1})
    assert res.status == "error" and res.code == INTERNAL, res.data
    assert pj.read_text(encoding="utf-8") == "{ 这不是合法 json", "不得覆写损坏的 project.json"


def test_publish_merges_instead_of_overwriting(tmp_path):
    """AG-4 正常路径不被破坏：合并后 event_seq/title 仍在。"""
    ws, pid = _ws(tmp_path)
    reg = _reg(ws, danger="allow")
    draft = ws.draft_path(pid, 1, 1)
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("正文", encoding="utf-8")

    res = reg.invoke(_sess(pid), "publish", {"vol": 1, "ch": 1})
    assert res.status == "ok", res.data
    saved = Checkpoint(ws).load(pid)
    assert saved["event_seq"] == 57 and saved["title"] == "长夜"
    assert saved["pipeline_state"] == "审查"


# ------------------------------------------------------- A-5 server 读配置

def test_server_reads_config_toml(tmp_path, monkeypatch):
    """P0-4：HTTP 服务此前 `load_config()` 无参 → config.toml 完全无效。"""
    cfg = tmp_path / "config.toml"
    cfg.write_text('[storage]\nworkspace_root = "server-root"\n', encoding="utf-8")
    monkeypatch.setenv("NOVELIST_CONFIG", str(cfg))

    import novelist.server as srv

    ws = srv._workspace()
    assert ws.root == "server-root", ws.root


def test_server_falls_back_to_cwd_when_no_config(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVELIST_CONFIG", str(tmp_path / "nope.toml"))
    import novelist.server as srv

    assert srv._workspace().root == "."
