"""RAG 知识检索层测试（讨论第 8 轮，M3i）。

覆盖：
- KnowledgeBase 收集：设定/人物/伏笔/教训/势力/物品 条目化
- 语义检索退化：keyword 模式下 retrieve = 关键词命中
- LLM 查询生成：plan_queries 产出查询词 / 无 provider 回退事件文本
- system 瘦身：人物卡全卡/势力/伏笔/教训不再全量进 system（人物名单行）
- 事件级注入：related 段含人物卡（带状态/首现标记）、设定、伏笔
- 最近 1 章记忆回退
"""

from __future__ import annotations

import json

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
        {"id": "char:ye", "name": "叶岚", "aliases": ["叶师弟"], "gender": "male",
         "is_protagonist": True, "core_traits": ["稳健"], "power": {"level": "炼气三层"},
         "first_appear": {"vol": 1, "ch": 1}},
        {"id": "char:shen", "name": "沈青梧", "aliases": [], "gender": "female",
         "core_traits": ["飒爽"], "power": {"level": "筑基中期"},
         "first_appear": {"vol": 1, "ch": 2}},
    ])
    _write(ws, pid, "bible/worldview.json", {
        "name": "青云界", "power_system": {"levels": ["炼气", "筑基", "金丹"]},
        "rules": ["修士不可对凡人出手"],
        "factions": [{"faction": "青云宗", "note": "主角所在宗门"},
                     {"faction": "血刀门", "note": "敌对势力"}]})
    _write(ws, pid, "bible/style.json",
           {"protagonist": {"id": "char:ye", "name": "叶岚", "gender": "male"}})
    _write(ws, pid, "bible/plot_threads.json", [
        {"id": "thread:yubi", "status": "planted", "desc": "断玉佩的来历与封印"}])
    _write(ws, pid, "bible/review_lessons.json", [
        {"_key": "战力越级|炼气震退筑基", "category": "战力越级",
         "rule": "不得让低境界者凭空碾压高境界者", "source": "1:1"}])
    _write(ws, pid, "bible/settings.json", [
        {"id": "set:wuwu", "keywords": ["五五开", "绑定", "修为一致"],
         "text": "五五开系统：绑定他人后修为与之一致。", "revealed": False, "first_ch": 1}])
    _write(ws, pid, "bible/worldstate.json", {"characters": {
        "char:ye": {"name": "叶岚", "realm": "炼气三层", "location": "外门",
                    "items": [], "injuries": [], "dead": False, "history": []}}})


class _SeqLLM:
    def __init__(self, rs):
        self.rs = list(rs)

    def complete(self, req):
        if not self.rs:
            return LLMResult(ok=True, content="", finish_reason="stop", provider="stub")
        return self.rs.pop(0)


def _res(text):
    return LLMResult(ok=True, content=text, finish_reason="stop", provider="stub")


# ---------------------------------------------------------------- KnowledgeBase


def test_kb_collects_all_kinds(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    from novelist.core.knowledge import KnowledgeBase

    kb = KnowledgeBase(ws, pid)
    kinds = {it.kind for it in kb._items}
    assert kinds >= {"setting", "character", "thread", "lesson", "faction"}
    assert len(kb._items) >= 7  # 2 人物 + 1 设定 + 1 伏笔 + 1 教训 + 2 势力


def test_kb_retrieve_keyword_mode(tmp_path):
    """keyword embedding 下 retrieve = 关键词命中 + 语义退化。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    from novelist.core.knowledge import KnowledgeBase

    kb = KnowledgeBase(ws, pid)  # 默认 KeywordEmbedding
    hits = kb.retrieve("叶岚在青云宗遇到血刀门弟子")
    kinds = {it.kind for it in hits}
    assert "character" in kinds and "faction" in kinds
    assert any("叶岚" in it.text for it in hits)


def test_kb_plan_queries_uses_llm_and_falls_back(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    from novelist.core.knowledge import KnowledgeBase

    kb = KnowledgeBase(ws, pid)
    # 无 provider → 回退事件文本
    assert kb.plan_queries("叶岚觉醒系统") == ["叶岚觉醒系统"]
    # 有 provider → LLM 查询词
    qs = kb.plan_queries("叶岚觉醒系统", _SeqLLM([_res("五五开, 绑定, 青云宗")]))
    assert qs == ["五五开", "绑定", "青云宗"]


def test_kb_lines_character_with_state_and_first_appear(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    from novelist.core.knowledge import KnowledgeBase

    kb = KnowledgeBase(ws, pid)
    items = [it for it in kb._items if it.kind == "character" and "叶岚" in it.text]
    lines = kb.lines(items, "character", vol=1, ch=1)
    assert lines and "叶岚" in lines[0]
    assert "当前修为 炼气三层" in lines[0], "状态随人物卡事件级注入"
    assert "本章首次出场" in lines[0]
    # ch2 不再是首现
    lines2 = kb.lines(items, "character", vol=1, ch=2)
    assert "本章首次出场" not in lines2[0]


# ---------------------------------------------------------------- system 瘦身


def test_system_slimmed_rag(tmp_path):
    """RAG 化后：全卡/势力/伏笔/教训不再全量进 system，保留名单行。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    from novelist.core.context import build_chapter_context

    sp = build_chapter_context(ws, pid, 1, 1).system_prompt
    assert "人物名单" in sp and "叶岚" in sp      # 名单行（防造人）
    assert "本章出场人物" not in sp                # 全卡移除
    assert "血刀门" not in sp                      # 势力不再前 N 全给
    assert "断玉佩的来历" not in sp                # 伏笔不在 system（事件级注入）
    assert "不得让低境界者" not in sp              # 教训不在 system（事件级注入）
    assert "修士不可对凡人出手" in sp              # 铁律保留（L1 基座）
    assert "炼气、筑基、金丹" in sp                # 境界体系保留（L1 基座）


# ---------------------------------------------------------------- 事件级注入


def test_event_goal_related_injection(tmp_path):
    """事件级 related 段：人物卡/设定/伏笔/教训按需注入。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    g = ws.outline_chapter_path(pid, 1, 1)
    g.parent.mkdir(parents=True, exist_ok=True)
    g.write_text("key_events: [叶岚绑定沈青梧，修为瞬间一致]\n", encoding="utf-8")
    from novelist.core.orchestrator import produce_chapter

    llm = _SeqLLM([
        _res("五五开, 沈青梧, 绑定"),                # plan_queries（LLM 查询生成）
        _res("叶岚与沈青梧绑定，修为升至筑基中期。"),  # gen
        _res("ok"),                                  # 审校
        _res("绑定 | turning_point | 叶岚,沈青梧"),  # 编纂
    ])
    res = produce_chapter(
        ws, pid, 1, 1, llm, prefer_direct=True, inject_bible=False,
        event_loop=True, commit_chapter_event=False, direct_words_floor=5,
        knowledge_llm=True, session=SessionInfo(project_id=pid, agent="t"),
        # ADR-020 默认开：本测试脚本队列不含其额外调用，显式关闭
        broadcast_casting=False,
        character_direction=False, perspective_memory=False, defer_title=False)
    assert res.ok, res.result


def test_recent_chapter_memory_fallback(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    _write(ws, pid, "memory/plot_events.json", [
        {"id": "e1", "at": {"vol": 1, "ch": 1}, "type": "discovery",
         "summary": "叶岚在藏经阁发现断玉佩", "participants": ["char:ye"], "affected_threads": []},
        {"id": "e2", "at": {"vol": 1, "ch": 2}, "type": "conflict",
         "summary": "血刀门弟子上门挑衅", "participants": ["char:ye"], "affected_threads": []},
    ])
    from novelist.core.orchestrator import _recent_chapter_memory

    lines = _recent_chapter_memory(ws, pid, 1, 3)
    assert lines and "血刀门弟子上门挑衅" in lines[0]  # 只取最近 1 章（ch2）
    assert "断玉佩" not in "".join(lines)
    assert _recent_chapter_memory(ws, pid, 1, 1) == []  # 无前章
