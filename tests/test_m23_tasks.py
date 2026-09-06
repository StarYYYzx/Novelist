"""M3x/ADR-028 任务板 TaskStore 测试（docs/06 §4.1 任务粒度 / CC s12 借鉴）。

覆盖（**纯确定性，不触 LLM**，docs/09 §2.1）：
- create/get/list 持久化往返（跨实例重建 = 磁盘恢复）
- start 编排器指派 owner + 防重入（同 owner 幂等 / 异主在途抛 TaskBusyError）
- can_start 前置依赖就绪检查（依赖未 done → blockers）
- complete 收尾解锁下游
- 崩溃恢复：章链中段 in_progress 崩溃 → recover(list) 扫出 / recover(redo) 回滚依赖链上游
- precheck 注入（承诺门衔接）：启动前判定 → blocked 并抛 TaskError
"""

from __future__ import annotations

import pytest

from novelist.core.tasks import (
    Task,
    TaskBusyError,
    TaskError,
    TaskStore,
    chapter_task_id,
    volume_task_id,
)


def _make_chain(ws, pid, vol: int, n: int):
    """建一个卷 + n 个连续章的线性依赖链，全部 pending；返回 store。"""
    store = TaskStore(ws, pid)
    store.create(Task(id=volume_task_id(vol), kind="volume", ref={"vol": vol},
                      title=f"卷{vol}", dependencies=[]))
    prev = volume_task_id(vol)
    for ch in range(1, n + 1):
        tid = chapter_task_id(vol, ch)
        store.create(Task(id=tid, kind="chapter", ref={"vol": vol, "ch": ch},
                          title=f"第{vol}-{ch}章", dependencies=[prev]))
        prev = tid
    return store


def test_persist_roundtrip_across_instances(ws_factory):
    """一部一文件：新实例能读到先例落的任务（= 崩溃后单任务恢复的基础）。"""
    ws, pid = ws_factory("proj-task-rt")
    TaskStore(ws, pid).create(
        Task(id=chapter_task_id(1, 1), kind="chapter", ref={"vol": 1, "ch": 1}, title="一章"))
    # 新实例：磁盘即事实源
    t = TaskStore(ws, pid).get(chapter_task_id(1, 1))
    assert t is not None and t.title == "一章" and t.status == "pending"


def test_start_assigns_owner_and_reentrant_same_owner(ws_factory):
    ws, pid = ws_factory("proj-task-own")
    store = _make_chain(ws, pid, 1, 1)
    tid = chapter_task_id(1, 1)
    store.start(tid, "文字匠")
    assert store.get(tid).owner == "文字匠"
    assert store.get(tid).status == "in_progress"
    store.start(tid, "文字匠")  # 同 owner 幂等
    assert store.get(tid).owner == "文字匠"


def test_busy_conflicting_owner_raises(ws_factory):
    ws, pid = ws_factory("proj-task-busy")
    store = _make_chain(ws, pid, 1, 1)
    tid = chapter_task_id(1, 1)
    store.start(tid, "文字匠")
    with pytest.raises(TaskBusyError):
        store.start(tid, "编纂员")  # 已被文字匠持锁 → 防重入


def test_can_start_requires_dependencies_done(ws_factory):
    ws, pid = ws_factory("proj-task-cs")
    store = _make_chain(ws, pid, 1, 3)
    ch2 = chapter_task_id(1, 2)
    ok, blockers = store.can_start(ch2)
    assert not ok and blockers, "第1章未 done → 第2章不能启动"
    assert blockers == [chapter_task_id(1, 1)]
    # 让上游 done 后解锁
    store.complete(chapter_task_id(1, 1))
    ok, blockers = store.can_start(ch2)
    assert ok and not blockers


def test_recover_list_finds_stalled(ws_factory):
    """崩溃恢复(list)：扫出 in_progress 未完成的半成品（触发单任务续写/重做决策）。"""
    ws, pid = ws_factory("proj-task-recover")
    store = _make_chain(ws, pid, 1, 3)
    store.complete(chapter_task_id(1, 1))
    store.start(chapter_task_id(1, 2), "文字匠")  # 写到一半崩溃，未 complete
    stalled = store.recover(policy="list")
    assert [t.id for t in stalled] == [chapter_task_id(1, 2)]
    # 下游第3章仍被挂起（上游未 done）
    assert not store.can_start(chapter_task_id(1, 3))[0]


def test_recover_redo_rolls_back_stalled_and_dependents(ws_factory):
    ws, pid = ws_factory("proj-task-redo")
    store = _make_chain(ws, pid, 1, 3)
    store.complete(chapter_task_id(1, 1))
    store.start(chapter_task_id(1, 2), "文字匠")
    affected = store.recover(policy="redo")
    ids = {t.id for t in affected}
    assert chapter_task_id(1, 2) in ids and chapter_task_id(1, 3) in ids, \
        "回退应覆盖半成品及其下游依赖"
    assert store.get(chapter_task_id(1, 2)).status == "pending"
    assert store.get(chapter_task_id(1, 2)).owner is None


def test_precheck_bridges_covenant_gate(ws_factory):
    """precheck 注入作为承诺账本衔接：启动前判定触碰承诺 → blocked 并抛 TaskError。"""
    ws, pid = ws_factory("proj-task-gate")
    store = _make_chain(ws, pid, 1, 1)
    tid = chapter_task_id(1, 1)
    with pytest.raises(TaskError):
        store.start(tid, "文字匠",
                    precheck=lambda t: (False, "触及承诺需人工闸门"))
    assert store.get(tid).status == "blocked"
    assert store.get(tid).meta.get("block_reason") == "触及承诺需人工闸门"


def test_semantic_guard_precheck_ok_allows_start(ws_factory):
    ws, pid = ws_factory("proj-task-gate-ok")
    store = _make_chain(ws, pid, 1, 1)
    store.start(chapter_task_id(1, 1), "文字匠",
                precheck=lambda t: (True, ""))
    assert store.get(chapter_task_id(1, 1)).status == "in_progress"