"""Forge 原子化落库 + 冲突裁决 + 容错解析回归（2026-09-16 真机事故）。

事故现场（`novel_workspace/proj-20260914233722`，用户 23:59 的 `/build`）：
`L0 book` 两次重试都因「主线不唯一（2 条）」失败，warn 只写了一句「回退父层产物」，
但**实际没有回退**——`_apply_threads` / `_apply_lines_skeleton` 在硬校验之前就已经
把产物 upsert 进蓝图，引擎随后照样 `bp.save()` + `sync_bible()`，于是 bible 里出现：

- `lines` 5 → 9 行，**两条同义主线**（`ln:main` / `ln:main_yinguo`，女主名还一个写苏晓一个写苏沐）
- `threads` 7 → 18 条（含 `pt:lingxiang_jinhua` 与 `pt:lingxiangjinhua` 归一后同名）
- `characters` 7 → 18 条（`char:linfeng` 与 `char:protagonist` 都叫林枫）

违反 docs/10「未通过校验的中间态不写入 bible/outline」。用户拍板：
① 结构冲突（第二条主线 / 近重复伏笔）**由用户裁决**——不自动改结构、也不整节点作废；
② L0 `book` 失败**即中止**本轮构建。本文件守住这两条 + 事务化落库。
"""

from __future__ import annotations

import json

import pytest

from novelist.core.normalize import (
    fix_structural_punctuation,
    loads_json_tolerant,
    strip_trailing_commas,
)
from novelist.forge.engine import build
from novelist.forge.genres import load_pack_for
from novelist.forge.nodes import NodeContext, _parse_node_reply, run_node, sync_bible
from novelist.forge.seed import _init_blueprint, _parse_seed_spec
from novelist.forge.state import Blueprint, blueprint_txn
from novelist.storage.workspace import Workspace

SEED_REPLY = json.dumps({
    "genre": "修仙", "template_suggestion": "修仙男频",
    "logline": "仙帝还因果", "protagonist_hint": {"name": "林枫", "gender": "male"},
    "conflict": "蓝星灵气稀薄", "tone_hint": "轻松诙谐",
    "scale_hint": {"volumes": 1, "chapters_per_volume": 1},
    "time_origin": "开书日", "unknowns": [],
}, ensure_ascii=False)


def _node_reply(artifact: dict) -> str:
    return json.dumps({"artifact": artifact, "decide": "done", "reason": "r"},
                      ensure_ascii=False)


def _main(lid: str, desc: str) -> dict:
    return {"id": lid, "desc": desc, "kind": "main", "carrier": "goal", "scope": "book",
            "members": ["char:linfeng"], "target": {"vol": 1, "note": "落点"}}


def _two_mains_artifact() -> dict:
    """复刻事故：模型把同一条主线写成两条（措辞几乎一致，只是女主名不同）。"""
    return {
        "worldview": {"name": "蓝星", "power_system": {"levels": ["凡", "灵相"]},
                      "rules": ["灵气稀薄"]},
        "characters": [{"id": "char:protagonist", "name": "林枫", "gender": "male",
                        "role": "protagonist", "core_traits": ["淡然"],
                        "background": "仙帝", "power": {"level": "仙帝"},
                        "arc": "还因果", "first_appear": {"vol": 1, "ch": 1}}],
        "threads": [{"id": "pt:yinguo", "desc": "因果是谁", "scope": "book", "target_vol": 1}],
        "lines": [_main("ln:main", "林枫为还因果培养苏晓成为蓝星最强"),
                  _main("ln:main_yinguo", "仙帝林枫为还因果将苏沐培养成蓝星最强")],
        "style": {"tense": "过去", "narration": "第三人称限知"},
    }


def _bp_with_one_main(ws: Workspace, pid: str, threads: int = 1) -> Blueprint:
    """建一个"重跑场景"的蓝图：账本里已有一条主线（事故前状态）。"""
    from novelist.core.lines import new_line

    spec = _parse_seed_spec(SEED_REPLY)
    bp = _init_blueprint(ws, pid, "brief", spec, load_pack_for("修仙男频"), "修仙男频", 1, 1, 1000)
    row = new_line("ln:main", "林枫为还因果培养苏晓", kind="main", carrier="goal",
                   scope="book", members=["char:linfeng"], target={"vol": 1, "note": "落点"})
    bp.upsert("lines", row)
    for i in range(threads):
        bp.upsert("threads", {"id": f"pt:old{i}", "desc": f"既有伏笔{i}",
                              "scope": "book", "status": "unplanned"})
    bp.save(ws, pid)
    sync_bible(ws, pid, bp)
    return bp


class _CaptureFixed:
    """固定回复 + 捕获请求（用于断言重试 prompt 里带了诊断）。"""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.prompts: list[str] = []

    def complete(self, req):
        from novelist.core.llm import LLMResult

        self.prompts.append(req.messages[-1].content)
        idx = min(len(self.prompts) - 1, len(self._replies) - 1)
        return LLMResult(ok=True, content=self._replies[idx], finish_reason="stop",
                         provider="capture")


# ---------------------------------------------------------------- 容错解析

def test_tolerant_json_repairs_trailing_comma_and_fullwidth():
    assert loads_json_tolerant('{"a": 1,}') == {"a": 1}
    assert loads_json_tolerant('{"a": [1, 2,]}') == {"a": [1, 2]}
    # 结构位全角标点（模型偶尔混用中文标点）
    assert loads_json_tolerant('{"a": 1，"b": 2}') == {"a": 1, "b": 2}
    assert loads_json_tolerant('{“a”: 1}') == {"a": 1}
    # 字符串**内**的全角标点必须原样保留
    assert loads_json_tolerant('{"a": "中文，标点：保留"}') == {"a": "中文，标点：保留"}


def test_tolerant_json_helpers_are_independent():
    assert strip_trailing_commas('{"a": [1,],}') == '{"a": [1]}'
    assert fix_structural_punctuation('[1，2]') == "[1,2]"
    assert fix_structural_punctuation('["中文，内容"]') == '["中文，内容"]'


def test_json_diagnostic_carries_line_and_excerpt():
    bad = '{\n  "a": 1\n  "b": 2\n}'
    with pytest.raises(ValueError) as ei:
        loads_json_tolerant(bad)
    msg = str(ei.value)
    assert "第 3 行" in msg
    assert '"b": 2' in msg


def test_parse_node_reply_reports_diagnostic():
    with pytest.raises(ValueError) as ei:
        _parse_node_reply('{"a": 1 "b": 2}')
    assert "第 1 行" in str(ei.value)
    # 可修复的（尾逗号）不该报错
    data = _parse_node_reply('{"artifact": {"x": 1,}, "reason": "r"}')
    assert data["artifact"] == {"x": 1}


# ---------------------------------------------------------------- 事务回滚

def test_blueprint_txn_rolls_back_on_error():
    bp = Blueprint.blank({"scale": {"volumes": 1}})
    bp.upsert("threads", {"id": "pt:a", "desc": "d"})
    before = json.dumps(bp.data, ensure_ascii=False)
    with pytest.raises(RuntimeError):
        with blueprint_txn(bp):
            bp.upsert("threads", {"id": "pt:b", "desc": "半成品"})
            bp.set_provenance("threads[pt:b].desc", "llm", 0.8)
            raise RuntimeError("模拟校验失败")
    assert json.dumps(bp.data, ensure_ascii=False) == before


# ---------------------------------------------------------------- 主线冲突：挂起裁决

def test_book_two_mains_suspends_conflict_instead_of_failing(tmp_path):
    """第二条主线**不抛错、不写入**，而是挂起等人工裁决（此前会整节点作废）。"""
    from novelist.forge.conflicts import open_conflicts

    ws = Workspace(root=str(tmp_path))
    pid = "p"
    ws.create_project(pid)
    bp = _bp_with_one_main(ws, pid, threads=2)
    threads_before = json.dumps(bp.section("threads"), ensure_ascii=False, sort_keys=True)

    ctx = NodeContext(ws=ws, project_id=pid, bp=bp, pack={},
                      provider=_CaptureFixed([_node_reply(_two_mains_artifact())]))
    warns = run_node(ctx, "book").warnings

    assert [x["id"] for x in bp.section("lines") if x.get("kind") == "main"] == ["ln:main"]
    assert all(x["id"] != "ln:main_yinguo" for x in bp.section("lines"))
    assert json.dumps(bp.section("threads"), ensure_ascii=False,
                      sort_keys=True) != threads_before  # 新伏笔正常新增
    items = open_conflicts(ws, pid, kind="lines")
    assert len(items) == 1
    assert items[0]["payload"]["candidate"]["id"] == "ln:main_yinguo"
    joined = " ".join(warns)
    assert "已挂起待人工裁决" in joined
    assert "/resolve cf:lines:1" in joined


def test_resolve_main_conflict_variants(tmp_path):
    """四类裁决都要生效，且裁决后 bible 主线仍唯一。"""
    from novelist.forge.conflicts import open_conflicts, resolve_conflict

    def _kinds(bp):
        return {x["id"]: x.get("kind") for x in bp.section("lines")}

    for choice in ("subplot", "replace", "merge", "keep-first"):
        ws = Workspace(root=str(tmp_path / choice))
        pid = "p"
        ws.create_project(pid)
        bp = _bp_with_one_main(ws, pid)
        ctx = NodeContext(ws=ws, project_id=pid, bp=bp, pack={},
                          provider=_CaptureFixed([_node_reply(_two_mains_artifact())]))
        run_node(ctx, "book")
        cid = open_conflicts(ws, pid, kind="lines")[0]["id"]
        assert resolve_conflict(ws, pid, cid, choice)
        after = Blueprint.load(ws, pid)
        if choice == "subplot":
            assert _kinds(after).get("ln:main_yinguo") == "subplot"
        elif choice == "replace":
            assert [x["id"] for x in after.section("lines")
                    if x["kind"] == "main"] == ["ln:main_yinguo"]
        elif choice == "merge":
            main = after.find_by_id("lines", "ln:main")
            assert "苏沐" in str(main.get("desc"))
            assert "ln:main_yinguo" not in _kinds(after)
        else:  # keep-first
            assert [x["id"] for x in after.section("lines")
                    if x["kind"] == "main"] == ["ln:main"]
            assert "ln:main_yinguo" not in _kinds(after)
        assert not open_conflicts(ws, pid, kind="lines")
        bible_lines = json.loads(ws.bible_path(pid, "lines").read_text(encoding="utf-8"))
        assert len([x for x in bible_lines if x.get("kind") == "main"]) == 1, choice


def test_thread_near_duplicate_suspended_and_resolvable(tmp_path):
    """归一后同名的伏笔同样挂起裁决（事故里 threads 7→18 的一半原因）。"""
    from novelist.forge.conflicts import open_conflicts, resolve_conflict

    ws = Workspace(root=str(tmp_path))
    pid = "p"
    ws.create_project(pid)
    bp = _bp_with_one_main(ws, pid)
    bp.upsert("threads", {"id": "pt:lingxiang_jinhua", "desc": "苏沐灵相吞噬进化的极限",
                          "scope": "book", "status": "unplanned"})
    art = {"threads": [{"id": "pt:lingxiangjinhua", "desc": "苏晓灵相为何能不断进化",
                        "scope": "book"}]}
    ctx = NodeContext(ws=ws, project_id=pid, bp=bp, pack={},
                      provider=_CaptureFixed([_node_reply(art)]))
    warns = run_node(ctx, "thread_set").warnings

    assert bp.find_by_id("threads", "pt:lingxiangjinhua") is None, "近重复候选不得直接落盘"
    items = open_conflicts(ws, pid, kind="threads")
    assert len(items) == 1 and "归一后同名" in items[0]["summary"]
    assert "已挂起待人工裁决" in " ".join(warns)

    resolve_conflict(ws, pid, items[0]["id"], "merge")
    kept = Blueprint.load(ws, pid).find_by_id("threads", "pt:lingxiang_jinhua")
    assert "另一说法" in kept["desc"] and "苏晓" in kept["desc"]
    assert not open_conflicts(ws, pid, kind="threads")


def test_resolve_keep_both_renames_candidate(tmp_path):
    from novelist.forge.conflicts import open_conflicts, resolve_conflict

    ws = Workspace(root=str(tmp_path))
    pid = "p"
    ws.create_project(pid)
    bp = _bp_with_one_main(ws, pid)
    bp.upsert("threads", {"id": "pt:lingxiang_jinhua", "desc": "灵相进化极限",
                          "scope": "book", "status": "unplanned"})
    art = {"threads": [{"id": "pt:lingxiangjinhua", "desc": "灵相为何能进化",
                        "scope": "book"}]}
    ctx = NodeContext(ws=ws, project_id=pid, bp=bp, pack={},
                      provider=_CaptureFixed([_node_reply(art)]))
    run_node(ctx, "thread_set")
    cid = open_conflicts(ws, pid, kind="threads")[0]["id"]
    resolve_conflict(ws, pid, cid, "keep-both")
    ids = [x["id"] for x in Blueprint.load(ws, pid).section("threads")]
    assert "pt:lingxiang_jinhua" in ids and "pt:lingxiangjinhua_b" in ids


def test_resolve_drop_new_keeps_only_existing(tmp_path):
    """threads 裁决 `drop-new`：候选丢弃、原伏笔**原样保留**（此前该分支零覆盖）。"""
    from novelist.forge.conflicts import load_conflicts, open_conflicts, resolve_conflict

    ws = Workspace(root=str(tmp_path))
    pid = "p"
    ws.create_project(pid)
    bp = _bp_with_one_main(ws, pid)
    bp.upsert("threads", {"id": "pt:lingxiang_jinhua", "desc": "灵相进化极限",
                          "scope": "book", "status": "unplanned"})
    art = {"threads": [{"id": "pt:lingxiangjinhua", "desc": "灵相为何能进化",
                        "scope": "book"}]}
    ctx = NodeContext(ws=ws, project_id=pid, bp=bp, pack={},
                      provider=_CaptureFixed([_node_reply(art)]))
    run_node(ctx, "thread_set")
    cid = open_conflicts(ws, pid, kind="threads")[0]["id"]

    note = resolve_conflict(ws, pid, cid, "drop-new")
    assert "丢弃候选" in note

    after = Blueprint.load(ws, pid)
    ids = [x["id"] for x in after.section("threads")]
    assert "pt:lingxiang_jinhua" in ids
    assert "pt:lingxiangjinhua" not in ids  # 无下划线的候选始终未落盘
    kept = after.find_by_id("threads", "pt:lingxiang_jinhua")
    assert kept["desc"] == "灵相进化极限", "drop-new 是丢弃，不得改动原伏笔内容"
    assert not open_conflicts(ws, pid, kind="threads")
    # 裁决留痕可追溯
    resolved = load_conflicts(ws, pid)["resolved"]
    assert resolved and resolved[-1]["choice"] == "drop-new" and resolved[-1]["id"] == cid


def test_resolve_rejects_choice_from_other_kind(tmp_path):
    """裁决取值跨 kind 混用会被拒（`lines` 的 `keep-first` 不适用于 threads）。"""
    from novelist.forge.conflicts import open_conflicts, resolve_conflict

    ws = Workspace(root=str(tmp_path))
    pid = "p"
    ws.create_project(pid)
    bp = _bp_with_one_main(ws, pid)
    bp.upsert("threads", {"id": "pt:a", "desc": "旧", "scope": "book"})
    art = {"threads": [{"id": "pt_a", "desc": "新", "scope": "book"}]}
    ctx = NodeContext(ws=ws, project_id=pid, bp=bp, pack={},
                      provider=_CaptureFixed([_node_reply(art)]))
    run_node(ctx, "thread_set")
    cid = open_conflicts(ws, pid, kind="threads")[0]["id"]
    with pytest.raises(ValueError, match="不支持"):
        resolve_conflict(ws, pid, cid, "keep-first")
    assert open_conflicts(ws, pid, kind="threads"), "被拒的裁决不得消耗待裁决项"


# ---------------------------------------------------------------- 构建级：不污染 + 中止

def test_build_does_not_persist_conflicting_writes(tmp_path):
    """构建级：bible 只保留一条主线，冲突挂起，且**不算节点失败**。"""
    ws = Workspace(root=str(tmp_path))
    pid = "p"
    ws.create_project(pid)
    bp = _bp_with_one_main(ws, pid, threads=2)
    existing_thread_ids = [x["id"] for x in bp.section("threads")]

    r = build(ws, pid, provider=_CaptureFixed([_node_reply(_two_mains_artifact())]),
              gate=False, deepen=False, max_calls=40)

    bible_lines = json.loads(ws.bible_path(pid, "lines").read_text(encoding="utf-8"))
    assert [x["id"] for x in bible_lines if x.get("kind") == "main"] == ["ln:main"]
    assert len(bible_lines) == 1, "冲突候选不得被写进 bible"
    assert r.nodes_failed == 0, "主线冲突不再算节点失败（改为挂起裁决）"
    joined = " ".join(r.warnings)
    assert "已挂起待人工裁决" in joined
    assert "回退父层产物" not in joined          # 不再宣称不存在的"回退"
    after_ids = [x["id"] for x in Blueprint.load(ws, pid).section("threads")]
    assert all(tid in after_ids for tid in existing_thread_ids)


def test_build_aborts_when_l0_book_fails(tmp_path):
    """拍板 2：L0 book 失败即中止（不再产出「新内容 + 旧骨架」的混血产物）。"""
    ws = Workspace(root=str(tmp_path))
    pid = "p"
    ws.create_project(pid)
    _bp_with_one_main(ws, pid)

    r = build(ws, pid, provider=_CaptureFixed(["这不是 JSON"]),
              gate=False, deepen=False, max_calls=40)

    assert r.ok is False and r.interrupted is True
    assert any("L0 book 未生成" in w for w in r.warnings), r.warnings
    assert r.calls_used <= 2, "L1/L2 一个都不该跑"


def test_build_aborts_after_consecutive_failures(tmp_path):
    """docs/10 §12：连续 3 个节点失败 → 中止并提示换 provider（此前完全没实现）。"""
    ws = Workspace(root=str(tmp_path))
    pid = "p"
    ws.create_project(pid)
    _bp_with_one_main(ws, pid)
    good_book = _node_reply({
        "worldview": {"name": "蓝星", "power_system": {"levels": ["凡"]}, "rules": ["灵气稀薄"]},
        "characters": [{"id": "char:linfeng", "name": "林枫", "gender": "male",
                        "role": "protagonist", "core_traits": ["淡然"], "background": "仙帝",
                        "power": {"level": "仙帝"}, "arc": "还因果",
                        "first_appear": {"vol": 1, "ch": 1}}],
        "threads": [{"id": "pt:yinguo", "desc": "因果是谁", "scope": "book"}],
        "lines": [_main("ln:main", "林枫为还因果培养苏晓")],
        "style": {"tense": "过去", "narration": "第三人称限知"},
    })
    r = build(ws, pid, provider=_CaptureFixed([good_book, "这不是 JSON"]),
              gate=False, deepen=True, max_calls=40)

    assert r.interrupted is True and r.ok is False
    assert any("连续 3 个节点" in w for w in r.warnings), r.warnings
    assert any("已回滚" in w for w in r.warnings)


def test_retry_prompt_carries_previous_failure(tmp_path):
    """重试必须把上一轮的错误（含行号上下文）写进 prompt——盲发重试等于赌运气。"""
    ws = Workspace(root=str(tmp_path))
    pid = "p"
    ws.create_project(pid)
    _bp_with_one_main(ws, pid)
    prov = _CaptureFixed(['{"lines": [1, 2,}', _node_reply(_two_mains_artifact())])
    build(ws, pid, provider=prov, gate=False, deepen=False, max_calls=40)
    assert len(prov.prompts) >= 2, "应当发生了一次重试"
    assert "上一轮输出不可用" not in prov.prompts[0]
    assert "上一轮输出不可用" in prov.prompts[1]
    assert "第 1 行" in prov.prompts[1]
