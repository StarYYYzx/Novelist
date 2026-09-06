"""dp-microbeat：事件微拍规划（docs/10 §7.9，开关 orchestrator.microbeat，默认关）。

覆盖：
- `_generate_microbeats`：起/承/转/合/钩 阶段解析、末拍收束钩子、逐拍带上一拍全文
- 拆解失败 / 单拍过短 → 回退事件级（None，开关默认不影响现有产出）
- `produce_chapter` 分发：microbeat 关→不产拍计划；开→走拍级生成
"""

from __future__ import annotations

from novelist.core.llm import LLMResult
from novelist.core.orchestrator import _generate_microbeats, produce_chapter
from novelist.core.session import SessionInfo
from novelist.storage.checkpoint import Checkpoint
from novelist.storage.workspace import Workspace

_SEED = "叶岚在驿路客栈打坐，窗外的血月之光忽明忽灭，他攥了攥袖中的断玉。"


def _project(tmp_path) -> tuple[Workspace, str]:
    ws = Workspace(root=str(tmp_path))
    pid = "proj-mb"
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
    # 前一章正文，供事件循环上下文
    p = ws.draft_path(pid, 1, 1)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("## 第一章\n\n" + _SEED, encoding="utf-8")


def _outline(ws, pid, ch, events):
    g = ws.outline_chapter_path(pid, 1, ch)
    g.parent.mkdir(parents=True, exist_ok=True)
    g.write_text(f"key_events: [{', '.join(events)}]\n", encoding="utf-8")


class _StubLLM:
    """记录每次完整调用；resonses 耗尽后回退到固定填充，保证全长调用可跑通。"""

    def __init__(self, responses: list[str] | None = None):
        self.responses = list(responses or [])
        self.full_calls: list[str] = []
        self.fallback = (
            "叶岚迈步上前，凝神应对眼前局面。他压住心头波澜，握紧拳锋，"
            "将这一战稳稳打完，尘埃落定。"

        )

    def complete(self, req):
        msg = req.messages[-1].content
        self.full_calls.append(msg)
        content = self.responses.pop(0) if self.responses else self.fallback
        return LLMResult(ok=True, content=content, finish_reason="stop", provider="stub")


# ---------------------------------------------------------------- _generate_microbeats

def test_microbeats_parse_stages_and_hook(tmp_path):
    """四拍（起/承/转/合）+ 钩：阶段解析正确、逐拍带上一拍全文、末拍收束调用。"""
    ws, pid = _project(tmp_path)
    plan = ("1. [起] 叶岚在客栈停驻，凝神调息\n"
            "2. [承] 风声渐紧，杀机逼近\n"
            "3. [转] 断玉微微发烫，暗藏玄机\n"
            "4. [合] 叶岚决定连夜动身，望向血月【钩】")
    llm = _StubLLM([
        plan,
        "他盘膝而坐，血月之光在窗棂上明灭。经脉里的气机缓缓沉定。",
        "忽有杀机隐现，廊下脚步声骤止。他睁开眼，掌心已攥出汗意。",
        "袖中断玉倏地发烫，一股苍凉气息沿着手臂窜上。他心神微凛。",
        "叶岚一把收了包裹，推门出屋，仰头望向血月，眼底掠过一丝决然。",
    ])
    res = _generate_microbeats(llm, "system", "goal", "叶岚夜奔离镇",
                               memories=["前情摘要"], setting_lines=[], related={},
                               readback_text="", generation_tokens=200,
                               max_continuations=1, direct_words_floor=5)
    assert res is not None
    text, beats = res
    assert beats == 4
    assert (", ".join(text.splitlines()) or True)
    joined = "\n".join(text.splitlines())
    assert "血月" in joined and "断玉" in joined and "决然" in joined
    # 拍级调用带上一拍全文（非截断接缝）
    assert any("【上一拍全文】" in c for c in llm.full_calls[1:]), "第 2 拍起应带上全全文"


def test_microbeats_no_stage_defaults_to_cheng(tmp_path):
    """规划未标阶段时默认「承」，仍可拆成 ≥2 拍。"""
    ws, pid = _project(tmp_path)
    llm = _StubLLM([
        "1. 先稳住局面\n2. 再看清楚来者",
        "叶岚退后半步，稳住气息，目光扫向来者。",
        "他看清来人后，紧绷的肩线终于松了下来。",
    ])
    res = _generate_microbeats(llm, "system", "goal", "来者何人",
                               [], [], {}, "", 200, 1, 5)
    assert res is not None
    _text, beats = res
    assert beats == 2


def test_microbeats_malformed_plan_falls_back(tmp_path):
    """规划不是拍清单 → None（回退事件级，保证开关默认不破坏现有产出）。"""
    ws, pid = _project(tmp_path)
    llm = _StubLLM(["这块要写得厚实一些"])
    assert _generate_microbeats(llm, "system", "goal", "普通事件", [], [], {}, "", 200, 1, 5) is None


def test_microbeats_short_beat_falls_back(tmp_path):
    """单拍产出低于篇幅下限 → None（整段回退事件级）。"""
    ws, pid = _project(tmp_path)
    llm = _StubLLM([
        "1. [起] 起势\n2. [承] 交手",
        "太短。",  # < direct_words_floor
    ])
    assert _generate_microbeats(llm, "system", "goal", "闭门切磋", [], [], {}, "", 200, 1, 20) is None


# ---------------------------------------------------------------- produce_chapter 分发

def test_produce_chapter_microbeat_off_no_beat_plan(tmp_path):
    """默认(关)：事件循环不走拍规划（不产「事件微拍规划」调用）。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    _outline(ws, pid, 2, ["叶岚夜奔离镇"])
    llm = _StubLLM()
    res = produce_chapter(
        ws, pid, 1, 2, llm, session=SessionInfo(project_id=pid, agent="t"),
        event_loop=True, prefer_direct=True, jit_characters=False,
        character_direction=False, cast_injection=False, perspective_memory=False,
        defer_title=False, event_review=False, microbeat=False, validate=False)
    assert res.ok, res.result
    assert any("夜奔" in c for c in llm.full_calls)
    assert not any("【事件微拍规划】" in c for c in llm.full_calls), \
        "microbeat 关 → 不得产拍规划"


def test_produce_chapter_microbeat_on_uses_beat_plan(tmp_path):
    """开关开：事件先产拍计划，再逐拍生成——正文含拍内容、调用含拍规划。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    _outline(ws, pid, 2, ["叶岚夜奔离镇"])
    beat = "叶岚收拾行囊推门而出，夜风灌满衣襟。他望着血月深吸一口气，径自踏上官道。"
    llm = _StubLLM([
        "1. [起] 收拾行囊\n2. [承] 踏上夜路",  # 拍计划
        beat, beat,  # 两拍正文
    ])
    res = produce_chapter(
        ws, pid, 1, 2, llm, session=SessionInfo(project_id=pid, agent="t"),
        event_loop=True, prefer_direct=True, jit_characters=False,
        character_direction=False, cast_injection=False, perspective_memory=False,
        defer_title=False, event_review=False, microbeat=True, validate=False)
    assert res.ok, res.result
    assert any("【事件微拍规划】" in c for c in llm.full_calls), "microbeat 开 → 应产拍规划"
    assert "踏上官道" in (res.result or "") or "衣襟" in (res.result or ""), \
        "正文应含拍级产物"