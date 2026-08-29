"""M2 里程碑测试（docs/08 M2）。

覆盖：一致性规则引擎（引用完整性/时间线单调）、CLI run 流水线推进、事件回写冲突双检、
DeepSeek 适配器（mock 解析 + 无 key 报错；真实 API 以 DeepSeek-API-KEY 存在与否 skipif）。
"""

from __future__ import annotations

import os
import json

import pytest

from novelist.consistency import run_consistency
from novelist.core.writeback import ContradictionError, LandedEvent, commit_event
from novelist.storage.checkpoint import Checkpoint
from novelist.storage.workspace import Workspace


def _new_project(tmp_path, pid="proj-m2"):
    ws = Workspace(root=str(tmp_path))
    ws.create_project(pid)
    Checkpoint(ws).save(pid, {"id": pid, "title": "t", "pipeline_state": "立项", "event_seq": 0})
    return ws, pid


def _write(ws, pid, rel, data):
    ws.write_json(ws._abs(f"{pid}/{rel}"), data)


# ---------- 一致性规则引擎 ----------


def test_rules_referential_integrity_reports_dangling(tmp_path):
    ws, pid = _new_project(tmp_path)
    _write(ws, pid, "bible/characters.json", [{"id": "char:a", "name": "A", "relationships": [{"target": "char:nonexist", "type": "mentor"}]}])
    alerts = run_consistency(ws, pid)
    dangling = [a for a in alerts if a.rule_id == "R-REF"]
    assert any("不存在的人物" in a.detail for a in dangling)


def test_rules_referential_integrity_ok(tmp_path):
    ws, pid = _new_project(tmp_path)
    _write(ws, pid, "bible/characters.json", [{"id": "char:a", "name": "A", "relationships": [{"target": "char:b", "type": "ally"}]}, {"id": "char:b", "name": "B"}])
    alerts = run_consistency(ws, pid)
    assert not [a for a in alerts if a.rule_id == "R-REF" and "不存在" in a.detail]


def test_rules_timeline_monotonic(tmp_path):
    ws, pid = _new_project(tmp_path)
    _write(ws, pid, "bible/timeline.json", [
        {"id": "tl:1", "at": {"year": 1}, "event": "早", "in_chapters": [{"vol": 2, "ch": 1}]},
        {"id": "tl:2", "at": {"year": 2}, "event": "晚", "in_chapters": [{"vol": 1, "ch": 1}]},  # 顺序倒退
    ])
    alerts = run_consistency(ws, pid)
    tl = [a for a in alerts if a.rule_id == "R-TL"]
    assert any("早于前一事件" in a.detail for a in tl)


# ---------- 事件回写冲突双检 ----------


def test_commit_event_contradiction_when_participant_unbuilt(tmp_path):
    ws, pid = _new_project(tmp_path)
    # characters.json 不存在/空 -> 有参与者应冲突
    ev = LandedEvent(project_id=pid, vol=1, ch=1, seq=1, kind="conflict", summary="战斗", participants=["char:c"])
    with pytest.raises(ContradictionError):
        commit_event(ev, session=None, ws=ws, validate=True)


def test_commit_event_appends_memory(tmp_path):
    ws, pid = _new_project(tmp_path)
    _write(ws, pid, "bible/characters.json", [{"id": "char:a", "name": "A"}])
    ev = LandedEvent(project_id=pid, vol=1, ch=1, seq=1, kind="turning_point", summary="苏晚继任", participants=["char:a"])
    assert commit_event(ev, session=None, ws=ws)
    # plot_events 落盘
    evt_file = ws._abs(f"{pid}/memory/plot_events.json")
    assert evt_file.exists()
    events = json.loads(evt_file.read_text(encoding="utf-8"))
    assert events[0]["summary"] == "苏晚继任"
    # 人物经历落盘
    hist = ws.char_history_path(pid, "char:a")
    assert hist.exists()


# ---------- CLI run 流水线推进 ----------


def test_cli_run_advances_pipeline(tmp_path):
    from click.testing import CliRunner

    from novelist.cli import cli

    ws, pid = _new_project(tmp_path)
    runner = CliRunner()
    res = runner.invoke(cli, ["run", str(tmp_path), "--to", "正文"])
    assert res.exit_code == 0, res.output
    assert "run" in res.output
    # project.json 已推进
    pj = json.loads(ws.project_json_path(pid).read_text(encoding="utf-8"))
    assert pj["pipeline_state"] == "正文"


# ---------- DeepSeek 适配器 ----------


def test_deepseek_requires_key(monkeypatch):
    from novelist.core.errors import ProviderError
    from novelist.providers.deepseek import DeepSeekProvider

    # 强制空 key（并清掉环境变量兜底）；构造后 complete 若无 key 应抛 ProviderError
    monkeypatch.delenv("DeepSeek-API-KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    p = DeepSeekProvider(api_key="")
    from novelist.core.llm import LLMMessage, LLMRequest

    with pytest.raises(ProviderError):
        p.complete(LLMRequest(messages=[LLMMessage(role="user", content="hi")]))


def test_parse_deepseek_ok():
    from novelist.providers.deepseek import parse_deepseek

    res = parse_deepseek(200, {"choices": [{"message": {"content": "好的"}, "finish_reason": "stop"}]})
    assert res.ok
    assert res.content == "好的"
    assert res.provider == "deepseek"


@pytest.mark.skipif(not os.getenv("DeepSeek-API-KEY"), reason="DeepSeek-API-KEY 未设置，跳过真实 API 集成")
def test_deepseek_live_completion():
    """真实 DeepSeek API 集成：设置 DeepSeek-API-KEY 后运行。"""
    from novelist.core.llm import LLMMessage, LLMRequest
    from novelist.providers.deepseek import DeepSeekProvider

    p = DeepSeekProvider(api_key=os.getenv("DeepSeek-API-KEY"), timeout_s=30)
    res = p.complete(LLMRequest(messages=[LLMMessage(role="user", content="用一句话自我介绍")]))
    assert res.ok
    assert res.content
