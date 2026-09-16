"""Forge 原子化落库 + 容错解析回归（2026-09-16 真机事故）。

事故现场（`novel_workspace/proj-20260914233722`，用户 23:59 的 `/build`）：
`L0 book` 两次重试都因"主线不唯一（2 条）"失败，warn 只写了一句「回退父层产物」，
但**实际没有回退**——`_apply_threads` / `_apply_lines_skeleton` 在硬校验之前就已经
把产物 upsert 进蓝图，引擎随后照样 `bp.save()` + `sync_bible()`，于是 bible 里出现：

- `lines` 5 → 9 行，**两条同义主线**（`ln:main` / `ln:main_yinguo`，女主名还一个写苏晓一个写苏沐）
- `threads` 7 → 18 条（含 `pt:lingxiang_jinhua` 与 `pt:lingxiangjinhua` 归一后同名）
- `characters` 7 → 18 条（`char:linfeng` 与 `char:protagonist` 都叫林枫）

这违反 docs/10「未通过校验的中间态不写入 bible/outline」。本文件守住修复后的行为。
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
from novelist.forge.nodes import NodeContext, _parse_node_reply, run_node
from novelist.forge.seed import _init_blueprint
from novelist.forge.genres import load_pack_for
from novelist.forge.seed import _parse_seed_spec
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
    out = loads_json_tolerant('{"a": "中文，标点：保留"}')
    assert out == {"a": "中文，标点：保留"}


def test_tolerant_json_helpers_are_independent():
    assert strip_trailing_commas('{"a": [1,],}') == '{"a": [1]}'
    assert fix_structural_punctuation('[1，2]') == "[1,2]"
    # 字符串内的全角不动
    assert fix_structural_punctuation('["中文，内容"]') == '["中文，内容"]'


def test_json_diagnostic_carries_line_and_excerpt():
    bad = '{\n  "a": 1\n  "b": 2\n}'
    try:
        loads_json_tolerant(bad)
    except ValueError as e:
        msg = str(e)
    else:  # pragma: no cover - 该输入必然失败
        raise AssertionError("应当解析失败")
    assert "第 3 行" in msg
    assert '"b": 2' in msg  # 摘录里带上了出错的那一行


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


# ---------------------------------------------------------------- 节点级：失败不留痕

def test_book_apply_two_mains_raises_and_leaves_blueprint_clean(tmp_path):
    """复刻事故：模型给出两条主线 → 硬校验抛错，但蓝图必须**一字未改**。"""
    ws = Workspace(root=str(tmp_path))
    pid = "p"
    ws.create_project(pid)
    bp = _bp_with_one_main(ws, pid, threads=2)
    lines_before = json.dumps(bp.section("lines"), ensure_ascii=False)
    threads_before = json.dumps(bp.section("threads"), ensure_ascii=False)

    ctx = NodeContext(ws=ws, project_id=pid, bp=bp, provider=None, pack={})
    ctx.provider = _CaptureFixed([_node_reply(_two_mains_artifact())])
    with pytest.raises(ValueError) as ei:
        run_node(ctx, "book")
    assert "主线不唯一" in str(ei.value)

    assert json.dumps(bp.section("lines"), ensure_ascii=False) == lines_before, "失败尝试的主线不得留在蓝图"
    assert json.dumps(bp.section("threads"), ensure_ascii=False) == threads_before, "失败尝试的伏笔不得留在蓝图"
    assert [x["id"] for x in bp.section("lines") if x.get("kind") == "main"] == ["ln:main"]


def test_build_does_not_persist_failed_node_partial_writes(tmp_path):
    """构建级：book 失败后，蓝图与 bible 的主线/伏笔必须与失败前完全一致。"""
    ws = Workspace(root=str(tmp_path))
    pid = "p"
    ws.create_project(pid)
    bp = _bp_with_one_main(ws, pid, threads=2)
    lines_before = json.dumps(bp.section("lines"), ensure_ascii=False, sort_keys=True)
    threads_before = json.dumps(bp.section("threads"), ensure_ascii=False, sort_keys=True)

    r = build(ws, pid, provider=_CaptureFixed([_node_reply(_two_mains_artifact())]),
              gate=False, deepen=False, max_calls=40)

    after = Blueprint.load(ws, pid)
    assert json.dumps(after.section("lines"), ensure_ascii=False,
                      sort_keys=True) == lines_before, "失败尝试的主线不得落进蓝图"
    assert json.dumps(after.section("threads"), ensure_ascii=False,
                      sort_keys=True) == threads_before, "失败尝试的伏笔不得落进蓝图"
    # bible 导出与蓝图一致，且主线唯一
    bible_lines = json.loads(ws.bible_path(pid, "lines").read_text(encoding="utf-8"))
    assert [x["id"] for x in bible_lines if x.get("kind") == "main"] == ["ln:main"]
    assert len(bible_lines) == 1
    bible_threads = json.loads(ws.bible_path(pid, "plot_threads").read_text(encoding="utf-8"))
    assert len(bible_threads) == 2
    assert r.nodes_failed >= 1
    # 文案必须说真话：不再宣称"回退父层产物"，而是"已回滚"
    joined = " ".join(r.warnings)
    assert "已回滚该节点的改动" in joined
    assert "回退父层产物" not in joined


def test_build_aborts_after_consecutive_failures(tmp_path):
    """docs/10 §12：连续 3 个节点解析失败 → 中止并提示换 provider（此前完全没实现）。"""
    ws = Workspace(root=str(tmp_path))
    pid = "p"
    ws.create_project(pid)
    _bp_with_one_main(ws, pid)

    r = build(ws, pid, provider=_CaptureFixed(["这不是 JSON"]),
              gate=False, deepen=False, max_calls=40)

    assert r.interrupted is True
    assert r.ok is False
    assert any("连续 3 个节点" in w for w in r.warnings), r.warnings
    assert any("已回滚" in w for w in r.warnings)


def test_retry_prompt_carries_previous_failure(tmp_path):
    """重试必须把上一轮的错误（含行号上下文）写进 prompt——盲发重试等于赌运气。"""
    ws = Workspace(root=str(tmp_path))
    pid = "p"
    ws.create_project(pid)
    _bp_with_one_main(ws, pid)
    good = _node_reply(_two_mains_artifact())   # JSON 合法，但仍会触发主线硬校验
    prov = _CaptureFixed(['{"lines": [1, 2,}', good])
    build(ws, pid, provider=prov, gate=False, deepen=False, max_calls=40)
    assert len(prov.prompts) >= 2, "应当发生了一次重试"
    assert "上一轮输出不可用" not in prov.prompts[0]
    assert "上一轮输出不可用" in prov.prompts[1]
    assert "第 1 行" in prov.prompts[1]   # 诊断带行号
