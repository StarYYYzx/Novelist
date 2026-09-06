"""dp-seam：状态锚接缝（docs/10 §7.10，开关 orchestrator.seam_state，默认关）。

覆盖：
- `_event_end_state`：把上一事件正文压缩成一句"结束时状态"；阻止/空输出 → None
- `produce_chapter(seam_state=True)`：事件接缝注入「上一事件结束时的状态」（事实），
  而非原文末尾（措辞）
- 默认(关)：绝不注入状态锚块（完全保持现有产出）
"""

from __future__ import annotations

from novelist.core.llm import LLMResult
from novelist.core.orchestrator import _event_end_state, produce_chapter
from novelist.core.session import SessionInfo
from novelist.storage.checkpoint import Checkpoint
from novelist.storage.workspace import Workspace

_STATE = "叶岚立于演武场中央，赵铁山负伤退场，四周人群喧哗未散，空气里还绷着一丝未尽的战意。"
_BODY = ("演武场尘土飞扬，叶岚收势而立，目光扫过台下。赵铁山抱拳认输，被师弟搀扶着"
         "退下，人群里响起一片议论声。叶岚垂眼，袖中的断玉却微微发烫起来。")


def _project(tmp_path) -> tuple[Workspace, str]:
    ws = Workspace(root=str(tmp_path))
    pid = "proj-seam"
    ws.create_project(pid)
    Checkpoint(ws).save(pid, {"id": pid, "pipeline_state": "正文"})
    return ws, pid


def _write(ws, pid, rel, data):
    ws.write_json(ws._abs(f"{pid}/{rel}"), data)


def _seed(ws, pid):
    _write(ws, pid, "bible/characters.json", [
        {"id": "char:zhu", "name": "叶岚", "gender": "male", "is_protagonist": True,
         "core_traits": ["稳健"], "power": {"level": "炼气三层"},
         "first_appear": {"vol": 1, "ch": 1}},
    ])
    _write(ws, pid, "bible/worldview.json",
           {"name": "青云界", "power_system": {"levels": ["炼气", "筑基", "金丹"]}})
    _write(ws, pid, "bible/style.json",
           {"protagonist": {"id": "char:zhu", "name": "叶岚", "gender": "male"},
            "tone": "严谨冷肃"})
    _write(ws, pid, "bible/plot_threads.json", [])
    p = ws.draft_path(pid, 1, 1)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("## 第一章\n\n叶岚在客栈打坐，窗外飘过血月之光。", encoding="utf-8")


def _outline(ws, pid, ch, events):
    g = ws.outline_chapter_path(pid, 1, ch)
    g.parent.mkdir(parents=True, exist_ok=True)
    g.write_text(f"key_events: [{', '.join(events)}]\n", encoding="utf-8")


class _StubLLM:
    def __init__(self, responses: list[str] | None = None):
        self.responses = list(responses or [])
        self.full_calls: list[str] = []
        self.fallback = (
            "叶岚迈步上前，凝神应对眼前局面。他压住心头波澜，握紧拳锋，"
            "稳稳打完这一场，尘埃落定。"
        )

    def complete(self, req):
        msg = req.messages[-1].content
        self.full_calls.append(msg)
        content = self.responses.pop(0) if self.responses else self.fallback
        return LLMResult(ok=True, content=content, finish_reason="stop", provider="stub")


# ---------------------------------------------------------------- _event_end_state

def test_event_end_state_compresses(tmp_path):
    ws, pid = _project(tmp_path)
    llm = _StubLLM([_STATE])
    s = _event_end_state(llm, "system", "赵铁山挑战叶岚", _BODY)
    assert s == _STATE
    assert any("结束时" in c for c in llm.full_calls), "prompt 应要求产结束时状态"


def test_event_end_state_blocked_returns_none(tmp_path):
    ws, pid = _project(tmp_path)
    llm = _StubLLM(["   "])
    assert _event_end_state(llm, "system", "ev", _BODY) is None


# ---------------------------------------------------------------- produce_chapter 分发

def _run(ws, pid, llm, chan, seam_state):
    return produce_chapter(
        ws, pid, 1, 2, llm, session=SessionInfo(project_id=pid, agent="t"),
        event_loop=True, prefer_direct=True, jit_characters=False,
        character_direction=False, cast_injection=False, perspective_memory=False,
        defer_title=False, event_review=False, seam_state=seam_state,
        validate=False)


def test_produce_chapter_seam_state_off_no_state_block(tmp_path):
    """默认(关)：事件接缝走原文末尾，绝不注入状态锚块。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    _outline(ws, pid, 2, ["叶岚夜奔离镇"])
    llm = _StubLLM()
    res = _run(ws, pid, llm, "叶岚夜奔离镇", seam_state=False)
    assert res.ok, res.result
    assert not any("【上一事件结束时的状态】" in c for c in llm.full_calls), \
        "seam_state 关 → 不得注入状态锚块"


def test_produce_chapter_seam_state_on_injects_state_anchor(tmp_path):
    """开关开：第二个事件的接缝用「上一事件结束时的状态」承接。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    _outline(ws, pid, 2, ["叶岚擂台比试", "叶岚下台回房"])
    # 序列：事件1正文 → 结束状态锚 → 事件2正文；其余（编纂等）回退
    body1 = "叶岚在演武场收势而立，擂台尘埃落定，围观人群渐渐散去。"
    body2 = "叶岚凭着记忆穿过回廊，径直走回自己的厢房，合上门扉。"
    llm = _StubLLM([body1, _STATE, body2])
    res = _run(ws, pid, llm, "叶岚夜奔离镇", seam_state=True)
    assert res.ok, res.result
    # 状态锚出现在某个调用（事件2 prompt 的【上一事件结束时的状态】块）
    assert any("【上一事件结束时的状态】" in c for c in llm.full_calls), \
        "seam_state 开 → 事件接缝应注入状态锚块"
    assert any(_STATE[:8] in c for c in llm.full_calls), "状态锚文本应进入下一事件 prompt"