"""D7 回归：CLI 项目定位统一（chapter/status 等 _locate_project 系 vs forge _resolve_forge_target）。

背景（2026-09-03 新书 5 章实测）：`novelist chapter novel_workspace/proj-x` 失败——
_locate_project 只扫根下首层子目录，不认"目录参数直指项目目录"；多项目工作区还
报 "target one explicitly" 却无参数可传。修复 = 抽 `_resolve_project(ws, directory)`
（与 forge 同语义：直指项目目录/根均可，ws 必要时重绑父目录），11 处调用点统一。
"""

from __future__ import annotations

import pytest
from click import ClickException

from novelist.cli import _locate_project, _resolve_forge_target, _resolve_project
from novelist.storage.checkpoint import Checkpoint
from novelist.storage.workspace import Workspace


def _mk_project(tmp_path, pid: str) -> Workspace:
    ws = Workspace(root=str(tmp_path))
    ws.create_project(pid)
    Checkpoint(ws).save(pid, {"id": pid, "title": pid, "pipeline_state": "立项", "event_seq": 0})
    return ws


def test_locate_project_single(tmp_path):
    _mk_project(tmp_path, "proj-a")
    assert _locate_project(tmp_path) == "proj-a"


def test_locate_project_multiple_raises(tmp_path):
    _mk_project(tmp_path, "proj-a")
    _mk_project(tmp_path, "proj-b")
    with pytest.raises(ClickException, match="multiple projects"):
        _locate_project(tmp_path)


def test_resolve_project_directory_points_at_project(tmp_path):
    """D7 原始故障：目录参数直指项目目录（老 chapter 路径 400 定位失败）。"""
    _mk_project(tmp_path, "proj-a")
    ws, pid = _resolve_project(Workspace(root=str(tmp_path)), str(tmp_path / "proj-a"))
    assert pid == "proj-a"
    # ws 重绑到项目父目录：项目内路径不再翻倍
    assert (ws._abs("proj-a") / "project.json").exists()


def test_resolve_project_directory_points_at_root(tmp_path):
    _mk_project(tmp_path, "proj-a")
    ws, pid = _resolve_project(Workspace(root="/"), str(tmp_path))
    assert pid == "proj-a"
    assert (ws._abs("proj-a") / "project.json").exists()


def test_resolve_project_default_unique(tmp_path):
    _mk_project(tmp_path, "proj-a")
    ws, pid = _resolve_project(Workspace(root=str(tmp_path)), None)
    assert pid == "proj-a" and ws.root == str(tmp_path)


def test_resolve_forge_target_same_semantics(tmp_path):
    """forge 通道与通用定位行为一致（单点语义，D7 收口）。"""
    _mk_project(tmp_path, "proj-a")
    ws, pid = _resolve_forge_target(Workspace(root=str(tmp_path)), str(tmp_path / "proj-a"))
    assert pid == "proj-a"
