"""D3 实证探针（不参与常规收集）：chapter 节点若严格照 prompt 输出（顶层无 artifact 包裹），
落盘细纲是否变占位。手动跑：pytest tests/_probe_d3_artifact.py -q -s
"""
import json

from novelist.core.bible import parse_gist
from novelist.forge import run_seed
from novelist.providers.fake import ScriptedProvider

from test_m13_forge_f1 import SEED_REPLY, BOOK_ARTIFACT, _volume_artifact


def _chapter_literal(ch: int) -> str:
    """模型"严格照 prompt"的产物：顶层直接是 title/key_events，无 artifact/decide。"""
    return json.dumps({
        "title": f"真章题{ch}", "pov": "第三人称限知（叶蓝视角）",
        "key_events": [f"真事件{ch}-1", f"真事件{ch}-2"], "turns": [f"真转折{ch}"],
        "characters": ["char:yelan"], "threads_involved": ["pt:yunwen"], "after_days": 0,
    }, ensure_ascii=False)


def test_probe_d3_chapter_literal_output(ws_factory, capsys):
    ws, pid = ws_factory("proj-d3")
    script = [{"final": SEED_REPLY}, {"final": json.dumps(
        {"artifact": BOOK_ARTIFACT, "decide": "done", "reason": "r"}, ensure_ascii=False)}]
    for v in (1, 2):
        script.append({"final": json.dumps(
            {"artifact": _volume_artifact(v), "decide": "done", "reason": "r"}, ensure_ascii=False)})
        if v == 1:
            script.append({"final": _chapter_literal(1)})
            script.append({"final": _chapter_literal(2)})
    res = run_seed(ws, pid, "五五开系统修仙文", provider=ScriptedProvider(script),
                   volumes=2, chapters_per_volume=2, target_words=1000, deepen=False)
    print("run_seed ok=", res.ok)
    print("warnings=", res.warnings[:6] if hasattr(res, "warnings") else res)
    g = parse_gist(ws, pid, 1, 1)
    print("gist 1-1 =", json.dumps(g, ensure_ascii=False))
    assert g is None or g.get("title") == "真章题1", (
        "细纲未按 prompt 顶层输出落盘——协议断裂实证：模型给了真事件，落盘却是占位/空"
    )
