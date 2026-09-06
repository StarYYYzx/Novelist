"""承诺账本（forge/covenant.py）测试：汇聚 + 确定性触碰判定 + 账本往返。

承诺账本不含 LLM，纯确定性逻辑（docs/09 §2.1 纪律）。
"""

from __future__ import annotations

import copy

from novelist.forge import Blueprint
from novelist.forge.covenant import (
    affected_modules,
    build_covenant,
    load_covenant,
    save_covenant,
    touched_entries,
)


def _bp(threads=None, volumes=None, characters=None) -> Blueprint:
    bp = Blueprint.blank({"title": "t", "genre": "修仙", "logline": "五五开系统"})
    bp.data["threads"] = threads or []
    bp.data["volumes"] = volumes or []
    bp.data["characters"] = characters or []
    return bp


def _thread(tid, status, target_vol=None, desc="伏笔", **kw):
    d = {"id": tid, "desc": desc, "status": status, "scope": "book"}
    if target_vol is not None:
        d["target_vol"] = target_vol
    d.update(kw)
    return d


def _vol(vol, payoff=None, summary="主线", **kw):
    d = {"vol": vol, "summary": summary, "threads_to_payoff": payoff or []}
    d.update(kw)
    return d


def _char(cid, name, role, **kw):
    return {"id": cid, "name": name, "role": role, **kw}


# ---------------------------------------------------------------------------
# 汇聚
# ---------------------------------------------------------------------------

def test_build_includes_committed_threads_volume_and_core_char():
    bp = _bp(
        threads=[
            _thread("t1", "planted", target_vol=2, desc="红莲秘境"),
            _thread("t2", "pending_return", desc="师门旧怨"),
            _thread("t3", "returned", desc="已回收"),       # 已回收不承诺
            _thread("t4", "unplanned", desc="未埋设"),       # 未承诺
        ],
        volumes=[_vol(1, payoff=["t1", "t2"], summary="卷一主线")],
        characters=[
            _char("c1", "主角", "protagonist"),
            _char("c2", "龙套", "minor"),
        ],
    )
    entries = build_covenant(bp)
    keys = {e.key for e in entries}

    assert "thread:t1" in keys   # planted 承诺
    assert "thread:t2" in keys   # pending_return 承诺
    assert "thread:t3" not in keys  # returned 不算在途
    assert "thread:t4" not in keys  # unplanned 未承诺
    assert "volume:1" in keys
    assert "character:c1" in keys
    assert "character:c2" not in keys  # 配角非核心

    t1 = next(e for e in entries if e.key == "thread:t1")
    assert set(t1.guard_fields) == {"status", "target_vol"}
    assert t1.anchor == "预计 vol 2"

    ch = next(e for e in entries if e.key == "character:c1")
    assert "role" in ch.guard_fields and "status" in ch.guard_fields


def test_default_bp_empty_entry_topology():
    assert build_covenant(_bp()) == []


# ---------------------------------------------------------------------------
# 触碰判定
# ---------------------------------------------------------------------------

def _ppair(changes=None, base_threads=None, base_volumes=None, base_chars=None):
    """构造一份"旧蓝图 + 修改后的新蓝图"。changes: (section, apply_fn) 列表。

    新旧蓝图各自独立深拷贝，保证改动只作用于新一侧（判定才有意义）。
    """
    old = _bp(copy.deepcopy(base_threads or []),
              copy.deepcopy(base_volumes or []),
              copy.deepcopy(base_chars or []))
    new = _bp(copy.deepcopy(base_threads or []),
              copy.deepcopy(base_volumes or []),
              copy.deepcopy(base_chars or []))
    for sec, fn in (changes or []):
        fn(new.data[sec])
    return old, new


def test_no_change_touches_nothing():
    th = [_thread("t1", "planted", 2)]
    vl = [_vol(1, ["t1"])]
    ch = [_char("c1", "主角", "protagonist")]
    old, new = _ppair(base_threads=th, base_volumes=vl, base_chars=ch)
    assert touched_entries(old, new) == []


def test_changing_thread_target_vol_touches():
    th = [_thread("t1", "planted", 2)]
    def shift(items):
        items[0]["target_vol"] = 5
    old, new = _ppair(changes=[("threads", shift)], base_threads=th)
    touched = touched_entries(old, new)
    assert [e.key for e in touched] == ["thread:t1"]


def test_deleting_committed_thread_touches():
    th = [_thread("t1", "planted", 2), _thread("t2", "planted", 3)]
    def drop(items):
        items.remove(items[0])
    old, new = _ppair([("threads", drop)], base_threads=th)
    touched = touched_entries(old, new)
    assert [e.key for e in touched] == ["thread:t1"]


def test_unchanged_desc_does_not_touch():
    th = [_thread("t1", "planted", 2, desc="旧描述")]
    def reword(items):
        items[0]["desc"] = "新措辞"  # desc 不在守卫
    old, new = _ppair([("threads", reword)], base_threads=th)
    assert touched_entries(old, new) == []


def test_volume_summary_change_touches_but_title_does_not():
    vl = [_vol(1, ["t1"], summary="旧主线")]
    def edit_summary(items):
        items[0]["summary"] = "新主线"
    def edit_title(items):
        items[0]["title"] = "新标题"  # title 不在守卫
    _, n1 = _ppair([("volumes", edit_summary)], base_volumes=vl)
    assert [e.key for e in touched_entries(_bp([], vl), n1)] == ["volume:1"]

    _, n2 = _ppair([("volumes", edit_title)], base_volumes=vl)
    assert touched_entries(_bp([], vl), n2) == []


def test_core_char_role_change_touches_but_brand_new_core_does_not():
    # 已有核心承诺的角色（rival）被降级 → 触及承诺
    chars = [_char("c1", "反派", "rival"), _char("c2", "龙套", "minor")]
    def demote(items):
        for c in items:
            if c["id"] == "c1":
                c["role"] = "minor"
    old, new = _ppair([("characters", demote)], base_chars=chars)
    assert [e.key for e in touched_entries(old, new)] == ["character:c1"]

    # 原本非承诺的配角被提拔为核心 → 不属于"既存承诺"，不触发已承诺守卫
    def promote(items):
        for c in items:
            if c["id"] == "c2":
                c["role"] = "protagonist"
    old, new = _ppair([("characters", promote)], base_chars=chars)
    assert touched_entries(old, new) == []


def test_core_char_arc_change_touches():
    chars = [_char("c1", "主角", "protagonist", arc="黑化")]
    def rearc(items):
        items[0]["arc"] = "洗白"
    old, new = _ppair([("characters", rearc)], base_chars=chars)
    assert [e.key for e in touched_entries(old, new)] == ["character:c1"]


# ---------------------------------------------------------------------------
# 模块映射
# ---------------------------------------------------------------------------

def test_affected_modules_mapping():
    e_thread = _bp(threads=[_thread("t1", "planted", 2)])
    e_vol = _bp(volumes=[_vol(1, ["t1"])])
    e_char = _bp(characters=[_char("c1", "反派", "rival")])

    t = touched_entries(e_thread, _bp(threads=[_thread("t1", "planted", 5)]))
    v = touched_entries(e_vol, _bp(volumes=[_vol(1, ["t2"])]))
    c = touched_entries(e_char, _bp(characters=[_char("c1", "反派", "protagonist")]))

    assert affected_modules(t) == ["threads"]
    assert affected_modules(v) == ["volumes"]
    assert affected_modules(c) == ["characters"]
    assert affected_modules(t + v + c) == ["threads", "volumes", "characters"]


# ---------------------------------------------------------------------------
# 账本往返
# ---------------------------------------------------------------------------

def test_save_load_roundtrip(ws_factory):
    ws, pid = ws_factory()
    entries = build_covenant(_bp(
        threads=[_thread("t1", "planted", 2)],
        volumes=[_vol(1, ["t1"])],
        characters=[_char("c1", "主角", "protagonist")],
    ))
    path = save_covenant(ws, pid, entries)
    assert path.exists()

    back = load_covenant(ws, pid)
    assert [e.key for e in back] == [e.key for e in entries]
    assert back[0].guard_fields == entries[0].guard_fields


def test_load_rebuilds_from_blueprint(ws_factory):
    ws, pid = ws_factory()
    bp = _bp(threads=[_thread("t1", "planted", 2)])
    loaded = load_covenant(ws, pid, bp)
    assert [e.key for e in loaded] == ["thread:t1"]

    from novelist.forge.covenant import covenant_path

    assert covenant_path(ws, pid).exists()


def test_save_under_arbitrary_project_id(ws_factory):
    ws, _ = ws_factory()
    entries = build_covenant(_bp(threads=[_thread("t1", "planted", 2)]))
    save_covenant(ws, "custom-proj", entries)
    assert load_covenant(ws, "custom-proj")[0].key == "thread:t1"