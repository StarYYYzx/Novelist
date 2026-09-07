"""M3z 批次 B 测试（docs/03 ADR-030，草稿溯源 F2.6）。

覆盖（**单测绝不真调 LLM**，docs/09 §2.1）：
- build_source_list：cast / 线索 / 世界规则 / 设定 / 记忆基线聚合
- prompt 指纹非空且随内容变化
- write/read 往返 + 路径 `<vol>-<ch>.src.json`
- 脆弱性：bible 缺失/空项目不抛错，返回合法结构
- render_source_report 给出人读文本
- 接线：produce_chapter（direct 直出）成稿后自动落 src.json
"""

from __future__ import annotations

import json
import pathlib

import pytest

from novelist.core.draft_provenance import (
    build_source_list,
    draft_sources_path,
    read_source_list,
    render_source_report,
    write_source_list,
)
from novelist.core.orchestrator import produce_chapter
from novelist.core.session import SessionInfo
from novelist.storage.workspace import Workspace


def _put(ws, pid, rel, obj):
    p = pathlib.Path(str(ws._abs(pid))) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _full_ws(tmp_path) -> tuple[Workspace, str]:
    ws = Workspace(root=str(tmp_path))
    pid = "proj-b"
    ws.create_project(pid)
    _put(ws, pid, "project.json", {"id": pid, "title": "t", "pipeline_state": "世界观"})
    # context.load_bible：characters/plot_threads 是**裸列表**（非 {container:[...]} 包装）
    _put(ws, pid, "bible/characters.json",
         [{"id": "c1", "name": "苏晚", "is_protagonist": True,
           "first_appear": {"vol": 1, "ch": 1}},
          {"id": "c2", "name": "林昭", "is_protagonist": False,
           "first_appear": {"vol": 1, "ch": 1}}])
    _put(ws, pid, "bible/worldview.json",
         {"name": "沧海", "rules": ["灵气不可逆"],
          "power_system": {"levels": ["练气", "筑基"]}})
    _put(ws, pid, "bible/style.json",
         {"tone": ["热血"], "protagonist": {"id": "c1"}})
    _put(ws, pid, "bible/plot_threads.json",
         [{"id": "t1", "name": "复仇线", "status": "active"}])
    _put(ws, pid, "bible/settings.json",
         [{"term": "灵脉", "desc": "大地灵气脉络"}])
    # 记忆基线：一条真实事件 + 一条本章碎片的索引
    _put(ws, pid, "memory/plot_events.json",
         [{"type": "plot_event", "at": {"vol": 1, "ch": 1, "t": 1},
           "summary": "主角第一次踏入灵脉", "participants": ["c1"]}])
    _put(ws, pid, "memory/fragment_index.json",
         {"version": 1, "kind": "keyword", "revision": 1,
          "fragments": [{"sig": "f1", "type": "experience", "text": "开篇历练",
                         "source": {"vol": 1, "ch": 1}}]})
    return ws, pid


# ---------------------------------------------------------------------------
# build_source_list 聚合
# ---------------------------------------------------------------------------
def test_build_source_list_aggregates_sources(tmp_path):
    ws, pid = _full_ws(tmp_path)
    src = build_source_list(ws, pid, 1, 2, content="今日正文……",
                            system_prompt="你是主编剧。", goal="请写第 1 卷第 2 章。",
                            mode="direct")
    assert src["chapter"] == "1-2"
    assert src["vol"] == 1 and src["ch"] == 2
    assert src["content_chars"] == len("今日正文……")
    assert src["mode"] == "direct"
    # cast：主角 + 已登场人物（章节 1-2，两卡均在卷内已登场）
    names = {c["name"] for c in src["characters"]}
    assert {"苏晚", "林昭"} <= names
    # 线索账本
    assert any(t["id"] == "t1" and t["name"] == "复仇线" for t in src["plot_threads"])
    # 世界规则 + 力量体系
    assert any(r["text"] == "灵气不可逆" for r in src["world_rules"])
    assert src["power_system_levels"] == 2
    # 设定条目
    assert "灵脉" in src["settings"]
    # 记忆基线：近期事件 + 碎片计数（本章引用来自事件/碎片）
    assert src["memory"]["fragment_count"] == 1
    assert src["memory"]["recent_events"]
    # prompt 指纹非空且稳定
    assert src["prompt_fingerprint"].startswith("sha256:")
    assert len(src["prompt_fingerprint"]) == 7 + 64


def test_prompt_fingerprint_changes_with_prompt(tmp_path):
    ws, pid = _full_ws(tmp_path)
    a = build_source_list(ws, pid, 1, 2, content="x",
                          system_prompt="P1", goal="G1", mode="direct")
    b = build_source_list(ws, pid, 1, 2, content="x",
                          system_prompt="P2", goal="G1", mode="direct")
    assert a["prompt_fingerprint"] != b["prompt_fingerprint"]


def test_build_source_list_resilient_on_empty_workspace(tmp_path):
    ws = Workspace(root=str(tmp_path))
    pid = "proj-empty"
    ws.create_project(pid)
    _put(ws, pid, "project.json", {"id": pid, "title": "t", "pipeline_state": "世界观"})
    src = build_source_list(ws, pid, 1, 1, content="", provider=None, mode="tool")
    assert src["characters"] == []
    assert src["content_chars"] == 0
    assert src["memory"]["fragment_count"] == 0


# ---------------------------------------------------------------------------
# 落盘 / 读回 / 渲染
# ---------------------------------------------------------------------------
def test_write_read_roundtrip_and_path(tmp_path):
    ws, pid = _full_ws(tmp_path)
    p = draft_sources_path(ws, pid, 1, 2)
    assert p.name == "1-2.src.json"
    assert p == ws.draft_path(pid, 1, 2).with_name("1-2.src.json")

    src = build_source_list(ws, pid, 1, 2, content="c", system_prompt="S", goal="G",
                            mode="direct")
    write_source_list(ws, pid, 1, 2, src)
    assert p.exists()
    loaded = read_source_list(ws, pid, 1, 2)
    assert loaded is not None
    assert loaded["chapter"] == "1-2"
    assert loaded["prompt_fingerprint"] == src["prompt_fingerprint"]


def test_read_missing_returns_none(tmp_path):
    ws, pid = _full_ws(tmp_path)
    assert read_source_list(ws, pid, 9, 9) is None


def test_render_source_report(tmp_path):
    ws, pid = _full_ws(tmp_path)
    src = build_source_list(ws, pid, 1, 2, content="正文正文", system_prompt="S",
                            goal="G", mode="direct")
    write_source_list(ws, pid, 1, 2, src)
    text = render_source_report(read_source_list(ws, pid, 1, 2))
    assert "草稿 1-2" in text
    assert "prompt 指纹" in text
    assert "人物卡" in text and "苏晚" in text
    assert "线索" in text and "复仇线" in text
    assert "世界规则" in text and "灵气不可逆" in text
    assert "记忆" in text


def test_render_missing_is_helpful():
    assert "无源清单" in render_source_report(None)


# ---------------------------------------------------------------------------
# 接线：produce_chapter 成稿后自动落源清单
# ---------------------------------------------------------------------------
def test_produce_chapter_writes_source_list_direct(tmp_path):
    from tests.conftest import StubLLM

    chapter_text = ("第一章。他在晨曦里推开木门，看见整座灵脉在雾气中苏醒。"
                    "他握紧拳头，把这世道欠他的，一分分讨回来。")
    ws = Workspace(root=str(tmp_path))
    pid = "proj-wire"
    ws.create_project(pid)
    _put(ws, pid, "project.json", {"id": pid, "title": "t", "pipeline_state": "细纲"})

    res = produce_chapter(
        ws, pid, vol=1, ch=1, provider=StubLLM(chapter_text),
        session=SessionInfo(project_id=pid, agent="orchestrator"),
        prefer_direct=True, inject_bible=False, validate=False,
        jit_characters=False, broadcast_casting=False,
        system_prompt="你是主编剧。", goal_prefix="请写第一章。",
        final_goal="请写第一章，收束句。")
    assert res.ok is True, res.result
    assert ws.draft_path(pid, 1, 1).exists()
    src_path = draft_sources_path(ws, pid, 1, 1)
    assert src_path.exists(), "成稿后应自动落源清单"
    src = read_source_list(ws, pid, 1, 1)
    assert src is not None
    assert src["chapter"] == "1-1"
    assert src["mode"] == "direct"
    assert src["content_chars"] == len(chapter_text)