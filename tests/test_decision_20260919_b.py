"""2026-09-19 决策 D-6…D-14 落地的钉死测试（第二批）。

覆盖：D-6 实然搬家、D-7 章节锁、D-11 口径、D-12 prompt 缺口（C-P1-1/2/3/4）、
D-13 长对话压缩、D-14 console 流式输出。
"""
from __future__ import annotations

import json

from novelist.core.session import Budget, SessionInfo
from novelist.providers.fake import FakeProvider


# ---------- D-6：实然文件迁 memory/ ----------

def test_entity_progress_written_to_memory(ws_factory):
    from novelist.core.entity import EntityTracker

    ws, pid = ws_factory("proj-d6")
    t = EntityTracker.load(ws, pid)
    t.save()
    assert ws.memory_path(pid, "entity_progress").exists()
    assert not ws.bible_path(pid, "entity_progress").exists()


def test_review_lessons_written_to_memory(ws_factory):
    """lessons 写入新位置；旧位置存在时写回旧位置（避免双份分裂）。"""
    ws, pid = ws_factory("proj-d6b")
    ws.memory_path(pid, "review_lessons").write_text("[]", encoding="utf-8")
    from novelist.core.context import _lessons_rel

    assert _lessons_rel(ws, pid) == "memory/review_lessons.json"


def test_contract_covers_new_files():
    from novelist.core.bible import BIBLE_CONTRACT

    rels = {rel for rel, _schema in BIBLE_CONTRACT}
    for rel in ("bible/skills.json", "bible/lines.json",
                "memory/relationships.json", "memory/relationship_ledger.json",
                "memory/entity_progress.json", "memory/review_lessons.json"):
        assert rel in rels, rel


# ---------- D-7：章节互斥锁 ----------

def test_chapter_lock_is_exclusive(ws_factory):
    from novelist.storage.workspace import ChapterBusyError

    ws, pid = ws_factory("proj-d7")
    with ws.chapter_lock(pid, 1, 1):
        try:
            with ws.chapter_lock(pid, 1, 1):
                raise AssertionError("同章不得并发生成")
        except ChapterBusyError:
            pass
    # 释放后可再取
    with ws.chapter_lock(pid, 1, 1):
        pass


def test_chapter_lock_stale_takeover(ws_factory, monkeypatch):
    """陈旧锁（>30 分钟）可抢占，避免进程崩溃后死锁。"""
    import os

    ws, pid = ws_factory("proj-d7b")
    lock_dir = ws._abs(f"{pid}/.locks")  # noqa: SLF001
    lock_dir.mkdir(parents=True, exist_ok=True)
    p = lock_dir / "ch-1-1.lock"
    p.write_text("99999", encoding="ascii")
    old = os.stat(p).st_mtime - 3600
    os.utime(p, (old, old))
    with ws.chapter_lock(pid, 1, 1):
        assert p.exists()


# ---------- D-12 C-P1-4：世界观基座补全 ----------

def test_worldview_base_lines_includes_new_blocks():
    from novelist.core.context import worldview_base_lines

    lines = worldview_base_lines({
        "name": "落霞界", "summary": "剑气纵横",
        "power_system": {"levels": ["练气", "筑基"]},
        "rules": ["不可杀同门"],
        "civilizations": ["大夏王朝"],
        "factions": [{"name": "青云宗"}],
        "systems": ["灵脉体系"],
        "realm_fluctuates": ["叶蓝"],
        "unavailable_states": ["封印"],
        "modern_words": ["手机"],
    })
    text = "\n".join(lines)
    for block in ("文明/阵营格局", "势力", "世界运行体系", "境界波动名单",
                  "不可用状态", "现代词禁令"):
        assert block in text, block


# ---------- D-12 C-P1-2：审校实然状态 ----------

def test_review_context_injects_live_state(ws_factory):
    ws, pid = ws_factory("proj-c12")
    ws.bible_path(pid, "worldstate").write_text(json.dumps({
        "time": {"now": 30, "origin_text": "穿越日"},
        "characters": {"char:a": {"name": "甲", "realm": "筑基三层",
                                  "location": "青云宗", "dead": False}},
    }, ensure_ascii=False), encoding="utf-8")
    from novelist.consistency.reviewer import review_context

    ctx = review_context(ws, pid, cast_ids=["char:a"])
    assert "实然状态" in ctx
    assert "第 30 天" in ctx and "筑基三层" in ctx


# ---------- D-12 C-P1-3：视角记录头尾窗口 ----------

def test_head_tail_keeps_both_ends():
    from novelist.core.chronicler import _head_tail

    text = "头" + "中" * 5000 + "尾"
    out = _head_tail(text, 2500)
    assert out.startswith("头") and out.endswith("尾")
    assert "中段略" in out


# ---------- D-13：长对话压缩 ----------

def test_chat_history_compaction():
    from novelist.core.agent_runner import AgentRunner

    runner = AgentRunner(FakeProvider(reply="答" * 400),
                         SessionInfo(project_id="p", agent="t"),
                         budget=Budget(max_tokens_out=100, max_rounds=5))
    runner.compact_chars = 1200
    runner.system("SYS")
    runner._messages.append(type(runner._messages[0])(role="user", content="快照"))  # noqa: SLF001
    for i in range(4):
        runner._messages.append(type(runner._messages[0])(  # noqa: SLF001
            role="user", content=f"问题{i}"))
        runner._messages.append(type(runner._messages[0])(  # noqa: SLF001
            role="assistant", content="答" * 400))
    before = len(runner._messages)
    runner._maybe_compact()
    assert len(runner._messages) < before, "超阈值应压缩中段"
    assert any("历史摘要" in (m.content or "") for m in runner._messages)
    # system 与首条快照保留
    assert runner._messages[0].role == "system"
    assert any(m.content == "快照" for m in runner._messages)
    assert runner.compacted_turns > 0


def test_compaction_disabled_by_default():
    from novelist.core.agent_runner import AgentRunner

    runner = AgentRunner(FakeProvider(), SessionInfo(project_id="p", agent="t"),
                         budget=Budget(max_tokens_out=100, max_rounds=5))
    assert runner.compact_chars == 0  # 非对话态不压缩


# ---------- D-14：console 流式输出 ----------

def test_cli_stream_forwards_incrementally():
    from novelist.forge.console import _CliStream

    seen: list[str] = []

    class _IO:
        def output(self, text):
            seen.append(text)

    st = _CliStream(_IO())
    st.write("第一行\n")
    assert seen == ["第一行"], "应按行即时转发（而非命令结束才刷出）"
    st.write("第二")
    assert seen == ["第一行"]  # 半行不输出
    st.write("行\n")
    assert seen == ["第一行", "第二行"]
    assert st.text() == "第一行\n第二行\n"


def test_cli_stream_accepts_bytes():
    """有调用方向 stdout 写 bytes（此前会 TypeError 打死整条命令）。"""
    from novelist.forge.console import _CliStream

    seen: list[str] = []

    class _IO:
        def output(self, text):
            seen.append(text)

    st = _CliStream(_IO())
    st.write(b"bytes-line\n")
    assert seen == ["bytes-line"]
