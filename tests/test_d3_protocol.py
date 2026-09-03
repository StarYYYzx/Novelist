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
    BOOK_ARTIFACT,
    _volume_artifact,
    _node_reply,
    _chapter_artifact,
)


def _chapter_literal(ch: int) -> str:
    """模型严格照 chapter prompt（nodes.py 输出协议）回复：顶层直接是业务键，无壳。"""
    return json.dumps({
        "title": f"真章题{ch}", "pov": "第三人称限知（叶蓝视角）",
        "key_events": [f"真事件{ch}-1", f"真事件{ch}-2"], "turns": [f"真转折{ch}"],
        "characters": ["char:yelan"], "threads_involved": ["pt:yunwen"], "after_days": 0,
    }, ensure_ascii=False)


def _script(*, chapter_reply):
    """按引擎真实调用顺序排脚本：seed → book → v1 → v1 的两章 → v2…（卷闸门）。"""
    script = [{"final": SEED_REPLY}, {"final": _node_reply(BOOK_ARTIFACT)}]
    for v in (1, 2):
        script.append({"final": _node_reply(_volume_artifact(v))})
        if v == 1:
            for c in (1, 2):
                script.append({"final": chapter_reply(c)})
    return script


def _run(ws_factory, pid_suffix: str, script):
    ws, pid = ws_factory(f"proj-d3-{pid_suffix}")
    res = run_seed(ws, pid, "五五开系统修仙文", provider=ScriptedProvider(script),
                   volumes=2, chapters_per_volume=2, target_words=1000, deepen=False)
    return ws, pid, res


def test_d3_literal_output_lands_full_gist(ws_factory):
    """裸 JSON 细纲必须完整落盘：title/key_events/turns 一个不丢（旧行为=全占位）。"""
    ws, pid, res = _run(ws_factory, "literal", _script(chapter_reply=_chapter_literal))
    assert res.ok
    g = parse_gist(ws, pid, 1, 1)
    assert g is not None, "模型按 prompt 顶层输出后细纲仍是占位——D3 协议断裂复现"
    assert g["title"] == "真章题1"
    assert g["key_events"] == ["真事件1-1", "真事件1-2"]
    assert g["turns"] == ["真转折1"]
    assert parse_gist(ws, pid, 1, 2)["title"] == "真章题2"


def test_d3_literal_output_has_no_reason_warning(ws_factory):
    """裸产物无 decide/reason 是主干正常形态：不得触发"缺 reason"协议告警。"""
    ws, pid, res = _run(ws_factory, "noreason", _script(chapter_reply=_chapter_literal))
    joined = "\n".join(getattr(res, "warnings", []) or [])
    assert "未给 reason" not in joined


def test_d3_shell_output_still_lands(ws_factory):
    """带壳输出（旁支/测试 helper 形状）不受影响——回归锚点。"""
    ws, pid, res = _run(ws_factory, "shell", _script(chapter_reply=lambda ch: _node_reply(_chapter_artifact(ch))))
    assert res.ok
    g = parse_gist(ws, pid, 1, 1)
    assert g["title"] == "章 1"
    assert g["key_events"] == ["事件1-1", "事件1-2"]
