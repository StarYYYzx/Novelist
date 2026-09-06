"""事件抽取质量闸门 + 同章合成事件消歧（2026-09-04 实证缺陷）。

实证（proj-20260903194907）：
- plot_events 混入「目标出现」类 4 字压线 discovery（无参与者无线索，RAG 纯噪声）；
- ch1 章节兜底合成事件「完成第 1 卷第 1 章」与新轮真实事件并存——失败轮的合成
  记录在重跑后不清除。

契约：
- 抽取：无参与者且摘要 <8 字的事件行丢弃（≥8 字或带参与者照常入库）；
- 提交：任何事件落库前，同章旧 type=chapter 合成事件一律清除（真实事件使其
  过时 / 合成事件自身幂等重写）；`drop_by_source` 的"合成事件保留"语义不变。
"""

from __future__ import annotations

import json

from novelist.core.chronicler import Chronicler
from novelist.core.session import SessionInfo
from novelist.core.writeback import LandedEvent, commit_event
from novelist.storage.checkpoint import Checkpoint
from novelist.storage.workspace import Workspace


class _StubLLM:
    def __init__(self, content: str):
        self._content = content

    def complete(self, request):  # noqa: ANN001
        class _R:
            content = self._content
            blocked = False
            block_reason = ""

        return _R()


def _project(tmp_path, pid="proj-eq"):
    ws = Workspace(root=str(tmp_path))
    ws.create_project(pid)
    Checkpoint(ws).save(pid, {"id": pid, "title": "测试书名", "pipeline_state": "正文",
                              "event_seq": 0})
    return ws, pid


def _events(ws, pid):
    return json.loads(ws._abs(f"{pid}/memory/plot_events.json").read_text(encoding="utf-8"))  # noqa: SLF001


# ------------------------------------------------------------ 抽取质量闸门


def test_short_no_participant_event_rejected(tmp_path):
    ws, pid = _project(tmp_path)
    c = Chronicler(ws, pid, llm=_StubLLM("目标出现 | discovery\n"))
    assert c.extract("正文略").events == []


def test_long_no_participant_event_kept(tmp_path):
    ws, pid = _project(tmp_path)
    text = "夜袭过后校园结界波动未平，全城修士都在议论灵潮 | discovery\n"
    c = Chronicler(ws, pid, llm=_StubLLM(text))
    ev = c.extract("正文略").events
    assert len(ev) == 1 and ev[0].kind == "discovery"


def test_short_with_participant_event_kept(tmp_path):
    """有参与者的短事件不算噪声（摘要简短但指向明确）。"""
    ws, pid = _project(tmp_path)
    c = Chronicler(ws, pid, llm=_StubLLM("苏晚突破 | state_change | 苏晚\n"))
    assert len(c.extract("正文略").events) == 1


# ------------------------------------------------------------ 合成事件消歧


def test_real_event_supersedes_stale_synthetic(tmp_path):
    """失败轮留下的「完成第X卷第Y章」在真实事件落库时被清除。"""
    ws, pid = _project(tmp_path)
    sess = SessionInfo(project_id=pid, agent="orchestrator")
    commit_event(LandedEvent(project_id=pid, vol=1, ch=1, seq=1, kind="chapter",
                             summary="完成第 1 卷第 1 章"), sess, ws=ws)
    commit_event(LandedEvent(project_id=pid, vol=1, ch=1, seq=2, kind="discovery",
                             summary="苏晚在校门口拾得断玉佩"), sess, ws=ws)
    evs = _events(ws, pid)
    assert [e["type"] for e in evs] == ["discovery"], "旧合成事件须被真实事件取代"


def test_synthetic_recommit_is_idempotent(tmp_path):
    ws, pid = _project(tmp_path)
    sess = SessionInfo(project_id=pid, agent="orchestrator")
    for _ in range(2):
        commit_event(LandedEvent(project_id=pid, vol=2, ch=3, seq=1, kind="chapter",
                                 summary="完成第 2 卷第 3 章"), sess, ws=ws)
    evs = [e for e in _events(ws, pid) if e["at"] == {"vol": 2, "ch": 3}]
    assert len(evs) == 1, "同章合成事件重复提交不得叠加"


def test_rollback_still_keeps_synthetic(tmp_path):
    """drop_by_source 的既有语义不回退：情节回滚时进度记录保留。"""
    from novelist.core.memory import rollback_chapter

    ws, pid = _project(tmp_path)
    sess = SessionInfo(project_id=pid, agent="orchestrator")
    commit_event(LandedEvent(project_id=pid, vol=1, ch=1, seq=1, kind="chapter",
                             summary="完成第 1 卷第 1 章"), sess, ws=ws)
    rollback_chapter(ws, pid, 1, 1)
    assert [e["type"] for e in _events(ws, pid)] == ["chapter"]


# ------------------------------------------------- 事件模式章末问题清单反馈重试


def _seed_chars(write, ws, pid):
    write(ws, pid, "bible/characters.json", [
        {"id": "char:yelan", "name": "叶岚", "gender": "male", "status": "active",
         "power": {"level": "炼气三层", "faction": "青云宗"},
         "core_traits": ["冷静"], "aliases": ["叶师兄"],
         "first_appear": {"vol": 1, "ch": 1}, "is_protagonist": True},
        {"id": "char:sw", "name": "苏晚", "gender": "female", "status": "active",
         "power": {"level": "炼气四层", "faction": "青云宗"},
         "core_traits": ["刚烈"], "aliases": ["苏师姐"],
         "first_appear": {"vol": 1, "ch": 1}},
    ])


def _seq_llm(replies):
    from novelist.core.llm import LLMResult

    class _Seq:
        def __init__(self):
            self.replies = list(replies)
            self.calls: list[str] = []

        def complete(self, req):  # noqa: ANN001
            self.calls.append(req.messages[-1].content if req.messages else "")
            if not self.replies:
                return LLMResult(ok=True, content="", finish_reason="stop", blocked=False)
            return LLMResult(ok=True, content=self.replies.pop(0),
                             finish_reason="stop", blocked=False)

    return _Seq()


def test_event_mode_chapter_repair_loop(ws_factory, write_json):
    """事件模式章末闭环：拼接后结尾截断 → 问题清单修复调用 → 问题减少才采纳。

    事件片段各自合格（事件级审校 ok），整章拼完才发现末尾被截断——
    direct 模式有重试闭环，事件模式此前只做确定性去重。"""
    from novelist.core.orchestrator import produce_chapter

    ws, pid = ws_factory()
    _seed_chars(write_json, ws, pid)
    gist = ws.outline_chapter_path(pid, 1, 1)
    gist.parent.mkdir(parents=True, exist_ok=True)
    gist.write_text(
        "---\nvol: 1\nch: 1\ntitle: 弃徒\n"
        "key_events: [叶岚下山拾玉, 叶岚与苏晚月下夜谈]\n---\n\n正文要点",
        encoding="utf-8")

    # 每事件 5 次：调度 → 正文 → 审校 → 编纂 → 视角；ev2 正文故意截断（逗号结尾）
    truncated = "入夜，两人在月下相对而坐，叶岚斟酌着开口试探玉佩的来历，"
    repaired = ("叶岚下了山，在山道旁拾起半枚焦黑玉佩，掌心发烫，他垂眼收进怀里。\n\n"
                "入夜，两人在月下相对。叶岚试探着问起玉佩，苏晚别开脸，只说不知。"
                "夜风掠过，谁都没有再开口。")
    llm = _seq_llm([
        # ---- event 1 ----
        "叶岚 | 冷静 | 话少先观察 | 不得自曝穿越\n苏晚 | 刚烈 | 暗中跟随 | 不得示弱",
        "叶岚下了山，在山道旁拾起半枚焦黑玉佩，掌心发烫，他垂眼收进怀里。",
        "ok",
        "拾玉 | discovery | 叶岚",
        "叶岚 | 警觉 | 玉佩蹊跷 | 苏晚：无\n苏晚 | 好奇 | 忍住不问 | 叶岚：更神秘",
        # ---- event 2 ----
        "叶岚 | 冷静 | 试探来历 | 不得说前世\n苏晚 | 刚烈 | 咬唇不语 | 不得泄密",
        truncated,
        "ok",
        "夜谈 | plot | 叶岚、苏晚",
        "叶岚 | 审慎 | 苏晚有隐瞒 | 苏晚：防备\n苏晚 | 挣扎 | 不能说 | 叶岚：愧疚",
        # ---- 章末修复（问题：结尾被截断）----
        repaired,
        # ---- 章末拟题 ----
        "月下试探",
    ])
    res = produce_chapter(
        ws, pid, 1, 1, llm, prefer_direct=True,
        inject_bible=False, event_loop=True, commit_chapter_event=False,
        knowledge_llm=False, event_polish=False, supplement_settings=False,
        broadcast_casting=False,
        session=SessionInfo(project_id=pid, agent="t"))
    assert res.ok, res.result
    # 修复调用确实发生，且带问题清单
    assert any("【本章正文存在以下问题" in c for c in llm.calls)
    # 修复稿被采纳：草稿以完整收束结尾，且包含修复新增的收束句
    draft = ws.draft_path(pid, 1, 1)
    text = draft.read_text(encoding="utf-8")
    assert text.rstrip().endswith("。")
    assert "夜风掠过" in text
    assert res.completeness.get("ends_properly") is True


def test_event_mode_no_repair_when_clean(ws_factory, write_json):
    """拼接后无问题 → 不发生修复调用（不浪费 LLM 调用）。"""
    from novelist.core.orchestrator import produce_chapter

    ws, pid = ws_factory()
    _seed_chars(write_json, ws, pid)
    gist = ws.outline_chapter_path(pid, 1, 1)
    gist.parent.mkdir(parents=True, exist_ok=True)
    gist.write_text(
        "---\nvol: 1\nch: 1\ntitle: 弃徒\n"
        "key_events: [叶岚下山拾玉]\n---\n\n正文要点",
        encoding="utf-8")
    llm = _seq_llm([
        "叶岚 | 冷静 | 话少先观察 | 不得自曝穿越",
        "叶岚下了山，在山道旁拾起半枚焦黑玉佩，掌心发烫，他垂眼收进怀里，转身下了山道。",
        "ok",
        "拾玉 | discovery | 叶岚",
        "叶岚 | 警觉 | 玉佩蹊跷 | 苏晚：无",
        "月下拾玉",
    ])
    res = produce_chapter(
        ws, pid, 1, 1, llm, prefer_direct=True,
        inject_bible=False, event_loop=True, commit_chapter_event=False,
        knowledge_llm=False, event_polish=False, supplement_settings=False,
        broadcast_casting=False,
        session=SessionInfo(project_id=pid, agent="t"))
    assert res.ok, res.result
    assert not any("【本章正文存在以下问题" in c for c in llm.calls)
