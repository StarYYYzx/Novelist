"""预算分离测试（第二批·人工审查，2026-09-01 落地；篇幅上限 2026-09-05 移除）。

覆盖：
- content_tokens 正文预算透传（直出路径 → LLMRequest.max_content_tokens）
- max_events_per_chapter 每章事件数上限（超限截断 + events_capped）
- min_event_words 单事件最小篇幅（低于下限判失败）
- 配置层：BudgetConfig.default_max_content_tokens 默认与 TOML 覆盖

注：length_cap_chars / _truncate_to_boundary / 目标篇幅 prompt 行已按用户
2026-09-05 拍板移除（上限从未 binding，短章根因在切片生成与收束倾向）。
"""

from __future__ import annotations

import json

from novelist.config import load_config
from novelist.core.orchestrator import produce_chapter
from novelist.providers.fake import FakeProvider
from novelist.storage.checkpoint import Checkpoint
from novelist.storage.workspace import Workspace


class RecordingProvider(FakeProvider):
    """FakeProvider + 请求记录（断言预算透传）。"""

    def __init__(self, reply: str = "ok") -> None:
        super().__init__(reply=reply)
        self.requests: list = []

    def complete(self, req):
        self.requests.append(req)
        return super().complete(req)


def _project(tmp_path) -> tuple[Workspace, str]:
    ws = Workspace(root=str(tmp_path))
    pid = "proj-t"
    ws.create_project(pid)
    Checkpoint(ws).save(pid, {"id": pid, "pipeline_state": "正文"})
    return ws, pid


# ---------------------------------------------------------------- content_tokens 透传

def test_content_tokens_passthrough_direct(tmp_path):
    ws, pid = _project(tmp_path)
    prov = RecordingProvider(reply="正文内容。" * 50)
    res = produce_chapter(ws, pid, 1, 1, prov, prefer_direct=True,
                          content_tokens=1500, generation_tokens=400,
                          jit_characters=False, validate=False)
    assert res.ok, res.result
    assert any(getattr(r, "max_content_tokens", None) == 1500 for r in prov.requests)


# ---------------------------------------------------------------- 事件数上限

def _seed_gist(ws, pid, events: list[str]):
    p = ws._abs(f"{pid}/outline/chapters/1-1.md")
    p.parent.mkdir(parents=True, exist_ok=True)
    fm = "---\nkey_events: [" + ", ".join(f'"{e}"' for e in events) + "]\n---\n"
    p.write_text(fm + "\n正文细纲说明", encoding="utf-8")


def test_events_capped(tmp_path):
    ws, pid = _project(tmp_path)
    _seed_gist(ws, pid, ["事件一", "事件二", "事件三"])
    prov = RecordingProvider(reply="这是足够长的事件正文内容，用来满足最小篇幅要求。" * 8)
    res = produce_chapter(ws, pid, 1, 1, prov, prefer_direct=True,
                          event_loop=True, max_events_per_chapter=2,
                          knowledge_llm=False, event_review=False,
                          jit_characters=False, min_event_words=1,
                          validate=False)
    assert res.ok, res.result
    assert res.events_capped == 1  # 3 个事件截掉 1 个


def test_events_not_capped_without_limit(tmp_path):
    ws, pid = _project(tmp_path)
    _seed_gist(ws, pid, ["事件一", "事件二"])
    prov = RecordingProvider(reply="这是足够长的事件正文内容，用来满足最小篇幅要求。" * 8)
    res = produce_chapter(ws, pid, 1, 1, prov, prefer_direct=True,
                          event_loop=True,  # max_events_per_chapter 不传
                          knowledge_llm=False, event_review=False,
                          jit_characters=False, min_event_words=1,
                          validate=False)
    assert res.ok, res.result
    assert res.events_capped == 0


# ---------------------------------------------------------------- 单事件最小篇幅

def test_min_event_words_rejects_short(tmp_path):
    ws, pid = _project(tmp_path)
    _seed_gist(ws, pid, ["事件一"])
    prov = RecordingProvider(reply="太短")  # 2 字 < 120
    res = produce_chapter(ws, pid, 1, 1, prov, prefer_direct=True,
                          event_loop=True, min_event_words=120,
                          knowledge_llm=False, event_review=False,
                          jit_characters=False, validate=False)
    assert res.ok is False
    assert "too short" in res.result


# ---------------------------------------------------------------- 配置层

def test_config_content_tokens_default(tmp_path):
    cfg = load_config()  # 无配置文件 → 默认
    assert cfg.budget.default_max_content_tokens == 3000


def test_config_content_tokens_toml(tmp_path):
    p = tmp_path / "novelist.toml"
    p.write_text("[budget]\ndefault_max_tokens_out = 2000\ndefault_max_content_tokens = 1500\n",
                 encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg.budget.default_max_tokens_out == 2000
    assert cfg.budget.default_max_content_tokens == 1500


# ---------------------------------------------------------------- F7.1 生成审计（reports/ + .index.db）


def _audit_rows(ws, pid, kind="produce_chapter"):
    """读 .index.db 审计日志中 kind 的全部行 [(ts, session, payload)]。"""
    from novelist.storage.indexdb import IndexDb

    db = IndexDb(str(ws.index_db_path(pid)))
    db.init()
    with db.connect() as conn:
        return conn.execute(
            "SELECT ts, session, payload FROM audit_log WHERE kind=? ORDER BY seq", (kind,)
        ).fetchall()


def test_generation_audit_dual_write(tmp_path):
    ws, pid = _project(tmp_path)
    prov = RecordingProvider(reply="这是正文内容。" * 30)
    res = produce_chapter(ws, pid, 1, 1, prov, prefer_direct=True,
                          jit_characters=False, validate=False)
    assert res.ok, res.result
    # reports/stats/generation-*.md（人读持久）
    gens = sorted(ws._abs(f"{pid}/reports/stats").glob("generation-*.md"))
    assert gens, "应写正文生成报告"
    text = gens[-1].read_text(encoding="utf-8")
    assert "正文生成报告 第 1 卷第 1 章" in text
    assert "tokens" in text and "估算成本" in text
    # .index.db audit_log（机器查，ADR-016 辅助索引）
    rows = _audit_rows(ws, pid)
    assert rows, "audit_log 应有 produce_chapter 记录"
    payload = json.loads(rows[-1][2])
    assert payload["ok"] is True and payload["calls"] >= 1
    assert payload["tokens_in"] >= 0 and payload["tokens_out"] >= 0
    assert payload["mode"] == "direct"


def test_generation_audit_on_failure(tmp_path):
    ws, pid = _project(tmp_path)

    class BoomProvider(FakeProvider):
        def complete(self, req):
            raise RuntimeError("模型爆炸")

    res = produce_chapter(ws, pid, 1, 1, BoomProvider(), prefer_direct=True,
                          jit_characters=False, validate=False)
    assert not res.ok
    rows = _audit_rows(ws, pid)
    assert rows, "失败路径也应写审计"
    payload = json.loads(rows[-1][2])
    assert payload["ok"] is False and "生成异常" in payload.get("note", "")
