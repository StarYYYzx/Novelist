"""D3 协议回归测试：主干节点（book/volume/chapter）严格照 prompt 输出裸 JSON（顶层无
artifact 包裹）时，解析器必须把顶层业务键整体当产物完整落盘（D3 修复：
_parse_node_reply 兼容裸产物形状，forge/nodes.py）。同时守住带壳输出不回归。
"""
import json

from novelist.core.bible import parse_gist
from novelist.forge import run_seed
from novelist.providers.fake import ScriptedProvider

from test_m13_forge_f1 import (
    SEED_REPLY,
    _node_reply,
    _build_script, _chapter_artifact,
)


def _chapter_literal(ch: int) -> str:
    """模型严格照 chapter prompt（nodes.py 输出协议）回复：顶层直接是业务键，无壳。"""
    return json.dumps({
        "title": f"真章题{ch}", "pov": "第三人称限知（叶蓝视角）",
        "key_events": [f"真事件{ch}-1", f"真事件{ch}-2"], "turns": [f"真转折{ch}"],
        "characters": ["char:yelan"], "threads_involved": ["pt:yunwen"], "after_days": 0,
    }, ensure_ascii=False)


def _prep(ws_factory, pid_suffix: str):
    """标准构建（事件先行：卷一产出事件流 → 切章物化）；用于取得可测项目。"""
    ws, pid = ws_factory(f"proj-d3-{pid_suffix}")
    res = run_seed(ws, pid, "五五开系统修仙文",
                   provider=ScriptedProvider([{"final": SEED_REPLY},
                                              *_build_script(volumes=2, chapters=2)]),
                   volumes=2, chapters_per_volume=2, target_words=1000,
                   deepen=False, gate=False)
    return ws, pid, res


def _run_chapter_node(ws, pid, reply_json: str, ch: int = 1):
    """直接调用 chapter 节点（2026-09-19 事件先行后，build 不再逐章调它）。

    本文件验的是**章节点协议解析与细纲落盘**（裸 JSON / 带壳 JSON），与谁调它无关；
    直接调节点比经 build 更聚焦，且不依赖事件流内容。
    """
    from novelist.forge.nodes import NodeContext, run_node
    from novelist.forge.state import Blueprint

    bp = Blueprint.load(ws, pid)
    ctx = NodeContext(ws=ws, project_id=pid, bp=bp,
                      provider=ScriptedProvider([{"final": reply_json}]),
                      pack={}, vol=1, ch=ch)
    return run_node(ctx, "chapter")


def test_d3_literal_output_lands_full_gist(ws_factory):
    """裸 JSON 细纲必须完整落盘：title/key_events/turns 一个不丢（旧行为=全占位）。"""
    ws, pid, res = _prep(ws_factory, "literal")
    assert res.ok
    assert _run_chapter_node(ws, pid, _chapter_literal(1)).ok
    g = parse_gist(ws, pid, 1, 1)
    assert g is not None, "模型按 prompt 顶层输出后细纲仍是占位——D3 协议断裂复现"
    assert g["title"] == "真章题1"
    assert g["key_events"] == ["真事件1-1", "真事件1-2"]
    assert g["turns"] == ["真转折1"]
    assert _run_chapter_node(ws, pid, _chapter_literal(2), ch=2).ok
    assert parse_gist(ws, pid, 1, 2)["title"] == "真章题2"


def test_d3_literal_output_has_no_reason_warning(ws_factory):
    """裸产物无 decide/reason 是主干正常形态：不得触发"缺 reason"协议告警。"""
    ws, pid, res = _prep(ws_factory, "noreason")
    node_res = _run_chapter_node(ws, pid, _chapter_literal(1))
    joined = "\n".join([*(getattr(res, "warnings", []) or []),
                        *(getattr(node_res, "warnings", []) or [])])
    assert "未给 reason" not in joined


def test_d3_shell_output_still_lands(ws_factory):
    """带壳输出（旁支/测试 helper 形状）不受影响——回归锚点。"""
    ws, pid, res = _prep(ws_factory, "shell")
    assert res.ok
    assert _run_chapter_node(ws, pid, _node_reply(_chapter_artifact(1))).ok
    g = parse_gist(ws, pid, 1, 1)
    assert g["title"] == "章 1"
    assert g["key_events"] == ["事件1-1", "事件1-2"]
