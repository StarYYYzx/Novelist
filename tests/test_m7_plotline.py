"""明暗线 + 审校闭环测试（讨论第 6/7 轮落地，M3h）。

覆盖：
- 卷主线注入：build_chapter_context 读 outline/volumes.json 注入【本卷主线】
- 人物首次出场提示：first_appear == 本章的人物卡片带提示
- 伏笔关联 + 状态流转：编纂员关键词匹配 affected_threads + planted→active
- 每事件审校+修订：block → 带建议重写该事件（events_revised）
- 经验回灌：block 沉淀 review_lessons.json + 注入【历史教训】段
"""

from __future__ import annotations

import json

from novelist.consistency.reviewer import Reviewer, ReviewIssue
from novelist.core.llm import LLMResult
from novelist.core.session import SessionInfo
from novelist.storage.checkpoint import Checkpoint
from novelist.storage.workspace import Workspace


def _project(tmp_path) -> tuple[Workspace, str]:
    ws = Workspace(root=str(tmp_path))
    pid = "proj-t"
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
        {"id": "char:qing", "name": "沈青梧", "gender": "female",
         "core_traits": ["飒爽"], "power": {"level": "筑基中期"},
         "first_appear": {"vol": 1, "ch": 2}},
    ])
    _write(ws, pid, "bible/worldview.json",
           {"name": "青云界", "power_system": {"levels": ["炼气", "筑基", "金丹"]}})
    _write(ws, pid, "bible/style.json",
           {"protagonist": {"id": "char:zhu", "name": "叶岚", "gender": "male"}})
    _write(ws, pid, "bible/plot_threads.json", [
        {"id": "thread:yubi", "status": "planted", "desc": "断玉佩的来历与封印"},
        {"id": "thread:shi", "status": "active", "desc": "血刀门在查叶岚的底细"},
    ])
    _write(ws, pid, "outline/volumes.json", [
        {"id": "vol:1", "vol": 1, "title": "青云试炼",
         "summary": "叶岚穿越觉醒五五开系统，在青云宗立足并揭开断玉佩秘密",
         "chapter_range": [1, 20], "target_words": 4000},
    ])


class _SeqLLM:
    def __init__(self, rs):
        self.rs = list(rs)

    def complete(self, req):
        if not self.rs:
            return LLMResult(ok=True, content="", finish_reason="stop", provider="stub")
        return self.rs.pop(0)


def _res(text):
    return LLMResult(ok=True, content=text, finish_reason="stop", provider="stub")


# ---------------------------------------------------------------- 卷主线注入


def test_volume_mainline_injected(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    from novelist.core.context import build_chapter_context

    ctx = build_chapter_context(ws, pid, 1, 2)
    assert "本卷主线" in ctx.system_prompt
    assert "五五开系统" in ctx.system_prompt  # volumes.json summary 注入


def test_volume_missing_skips(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    _write(ws, pid, "outline/volumes.json", [])  # 空卷列表
    # 没有卷主线时静默跳过，不抛错
    from novelist.core.context import build_chapter_context

    ctx = build_chapter_context(ws, pid, 1, 2)
    assert "本卷主线" not in ctx.system_prompt


# ---------------------------------------------------------------- 人物首现提示


def test_first_appear_marked(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    from novelist.core.context import build_chapter_context

    ctx = build_chapter_context(ws, pid, 1, 2)  # 沈青梧 first_appear = 1:2
    assert "沈青梧" in ctx.system_prompt  # 名单行（防造人）
    assert "本章首次出场" not in ctx.system_prompt  # 首现标记移到事件级（知识层）
    from novelist.core.knowledge import KnowledgeBase
    kb = KnowledgeBase(ws, pid)
    items = [it for it in kb._items if it.kind == "character" and "沈青梧" in it.text]
    lines = kb.lines(items, "character", vol=1, ch=2)
    assert lines and "本章首次出场" in lines[0]


# ---------------------------------------------------------------- 伏笔关联+流转


def test_chronicler_links_threads_and_activates(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    from novelist.core.chronicler import Chronicler, ExtractedEvent

    c = Chronicler(ws, pid, llm=None)
    evs = [ExtractedEvent(summary="叶岚在藏经阁发现断玉佩，上面有封印的气息",
                          kind="discovery", participant_ids=["char:zhu"])]
    rep = c.commit(evs, 1, 1)
    assert rep.threads_activated == 1  # 断玉佩 伏笔 planted→active
    threads = json.loads(ws._abs(f"{pid}/bible/plot_threads.json").read_text(encoding="utf-8"))
    by_id = {t["id"]: t for t in threads}
    assert by_id["thread:yubi"]["status"] == "active"
    assert "thread:yubi" in evs[0].threads  # affected_threads 已填充


def test_threads_not_reactivated_when_active(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    from novelist.core.chronicler import Chronicler, ExtractedEvent

    c = Chronicler(ws, pid, llm=None)
    evs = [ExtractedEvent(summary="血刀门弟子在宗门附近查探叶岚",
                          kind="conflict", participant_ids=["char:zhu"])]
    rep = c.commit(evs, 1, 1)
    assert rep.threads_activated == 0  # 已是 active，不再流转


# ---------------------------------------------------------------- 每事件审校+修订


def test_event_review_revises_block_and_sinks_lessons(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    g = ws.outline_chapter_path(pid, 1, 1)
    g.parent.mkdir(parents=True, exist_ok=True)
    g.write_text("key_events: [叶岚夜探藏经阁]\n", encoding="utf-8")

    # 调用序列：gen(有战力越级问题) → 审校(block) → 修订 → 编纂
    llm = _SeqLLM([
        _res("叶岚以炼气三层之身，一掌震退筑基中期的沈青梧。"),  # gen（战力越级）
        _res("block | 战力越级 | 炼气三层震退筑基中期不合理 | 改为周旋或借助外物"),  # 审校 block
        _res("叶岚借镇魂阵的威力周旋，勉强脱身，没有正面硬撼。"),  # 修订
        _res("夜探藏经阁 | discovery | 叶岚"),                  # 编纂
    ])
    from novelist.core.orchestrator import produce_chapter

    res = produce_chapter(
        ws, pid, 1, 1, llm, prefer_direct=True, inject_bible=False,
        event_loop=True, commit_chapter_event=False, direct_words_floor=5,
        knowledge_llm=False, session=SessionInfo(project_id=pid, agent="t"),
        # ADR-020 默认开：本测试脚本队列只编排了审校/修订路径，关闭其额外调用
        broadcast_casting=False,
        character_direction=False, perspective_memory=False, defer_title=False)
    assert res.ok, res.result
    assert res.events_revised == 1, "block 应触发 1 次事件重写"
    assert res.review_blocks == 1
    final = ws.draft_path(pid, 1, 1).read_text(encoding="utf-8")
    assert "周旋" in final and "震退" not in final, "修订稿应替换问题稿"

    # 经验沉淀
    lessons = json.loads(ws._abs(f"{pid}/bible/review_lessons.json").read_text(encoding="utf-8"))
    assert res.lessons_added >= 1
    assert any("战力越级" in l["category"] for l in lessons)


def test_lessons_injected_into_next_context(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    from novelist.core.orchestrator import _append_lessons, _lesson_lines

    issues = [ReviewIssue(level="block", category="战力越级",
                          detail="炼气三层震退筑基中期",
                          suggestion="不得让低境界者凭空碾压高境界者")]
    assert _append_lessons(ws, pid, issues, 1, 1) == 1
    lines = _lesson_lines(ws, pid)
    assert lines and any("战力越级" in l for l in lines)

    # RAG 化后（M3i）：教训不再进 system，由知识层按事件相关性检索注入
    from novelist.core.knowledge import KnowledgeBase

    kb = KnowledgeBase(ws, pid)
    hits = kb.retrieve("叶岚修为越级碾压筑基高手")
    lesson_lines = kb.lines(hits, "lesson")
    assert lesson_lines and any("碾压" in l for l in lesson_lines)
