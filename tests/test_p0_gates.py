"""P0 闸门测试（prompt 作用审计 P0-B/P0-C + P1-F 死参数）。

- P1-F：build_system_prompt 的 lessons 死参数已删（第 8 轮 RAG 化残留）
- P0-B：supplement_settings 闸门——新词一律进 settings_pending.json 待人工确认，
  不再自动入档（"强化符"反向追认通道关死）；CLI settings-pending --allow/--deny
- P0-C：chronicler 入库闸门——地点名册核对 / 境界变更对齐 / 近似事件去重
"""

import inspect
import json

from novelist.core.chronicler import Chronicler, ExtractedEvent
from novelist.storage.checkpoint import Checkpoint
from novelist.core.context import build_system_prompt
from novelist.core.llm import LLMResult
from novelist.core.orchestrator import _supplement_settings
from novelist.core.worldstate import init_from_bible
from novelist.storage.workspace import Workspace


def _project(tmp_path, pid="proj-p0"):
    ws = Workspace(root=str(tmp_path))
    ws.create_project(pid)
    Checkpoint(ws).save(pid, {"id": pid, "title": "测试书", "pipeline_state": "正文",
                              "event_seq": 0})
    return ws, pid


def _write(ws, pid, rel, data):
    ws.write_json(ws._abs(f"{pid}/{rel}"), data)


def _seed(ws, pid):
    _write(ws, pid, "bible/characters.json", [
        {"id": "char:sw", "name": "苏晚", "gender": "male", "status": "active",
         "power": {"level": "炼气三层", "faction": "青云宗"}, "is_protagonist": True},
    ])
    _write(ws, pid, "bible/worldview.json", {
        "name": "青冥界",
        "power_system": {"levels": ["炼气", "筑基", "金丹", "元婴"]},
    })
    _write(ws, pid, "bible/locations.json", [
        {"id": "loc:ws", "name": "外山"},
    ])


class _JsonStub:
    """按调用序返回固定 JSON 的 LLM 替身（supplement_settings 用）。"""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls = 0

    def complete(self, req):
        self.calls += 1
        return LLMResult(ok=True, content=self.replies.pop(0) if self.replies else "[]",
                         finish_reason="stop", blocked=False)


class _TextStub:
    """返回固定行格式文本的 LLM 替身（chronicler 用）。"""

    def __init__(self, reply: str) -> None:
        self.reply = reply

    def complete(self, req):
        return LLMResult(ok=True, content=self.reply, finish_reason="stop", blocked=False)


# ---------------------------------------------------------------- P1-F lessons 死参数


def test_system_prompt_no_lessons_param():
    """lessons 死参数已删（审计 §2.1：第 8 轮 RAG 化后收而不用）。"""
    assert "lessons" not in inspect.signature(build_system_prompt).parameters


# ---------------------------------------------------------------- P0-B 设定闸门


def test_supplement_new_terms_go_pending_not_settings(tmp_path):
    """名册外新词 → settings_pending.json，settings.json 不动（追认通道关死）。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    llm = _JsonStub([json.dumps([
        {"term": "玄天秘境", "text": "青云宗后山禁地", "keywords": ["玄天秘境"]},
    ], ensure_ascii=False)])
    n = _supplement_settings(ws, pid, "叶岚误入玄天秘境。", llm)
    assert n == 1
    settings = json.loads(ws._abs(f"{pid}/bible/settings.json").read_text(encoding="utf-8")) \
        if ws._abs(f"{pid}/bible/settings.json").exists() else []
    assert settings == []  # 绝不自动入档
    pending = json.loads(ws._abs(f"{pid}/bible/settings_pending.json").read_text(encoding="utf-8"))
    assert [p["term"] for p in pending] == ["玄天秘境"]
    assert "待人工确认" in pending[0]["reason"]


def test_settings_pending_cli_allow(tmp_path):
    """--allow 把 pending 条目转正进 settings.json（带 id），--deny 直接丢弃。"""
    from click.testing import CliRunner

    from novelist.cli import cli

    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    ws.write_json(ws._abs(f"{pid}/bible/settings_pending.json"), [
        {"term": "玄天秘境", "text": "青云宗后山禁地", "keywords": ["玄天秘境"], "reason": "x"},
        {"term": "废话设定", "text": "不要了", "keywords": [], "reason": "x"},
    ])
    r = CliRunner().invoke(cli, ["settings-pending", str(tmp_path),
                                 "--allow", "玄天秘境", "--deny", "废话设定"])
    assert r.exit_code == 0, r.output
    settings = json.loads(ws._abs(f"{pid}/bible/settings.json").read_text(encoding="utf-8"))
    assert settings[0]["term"] == "玄天秘境" and settings[0]["id"].startswith("setting:")
    assert "reason" not in settings[0]
    pending = json.loads(ws._abs(f"{pid}/bible/settings_pending.json").read_text(encoding="utf-8"))
    assert pending == []


def test_supplement_known_items_in_register(tmp_path):
    """items/skills 名册纳入已知词（原洞：物品不在核对范围，强化符钻的就是这里）。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    _write(ws, pid, "bible/items.json", [
        {"id": "item:ju", "name": "聚灵丹", "aliases": ["聚气丹"]},
    ])
    llm = _JsonStub([json.dumps([
        {"term": "聚气丹", "text": "别名", "keywords": []},
    ], ensure_ascii=False)])
    assert _supplement_settings(ws, pid, "叶岚服下一枚聚气丹。", llm) == 0
    assert not ws._abs(f"{pid}/bible/settings_pending.json").exists()


# ---------------------------------------------------------------- P0-C 编纂员闸门


def _chron(ws, pid, reply):
    init_from_bible(ws, pid)
    return Chronicler(ws, pid, llm=_TextStub(reply))


def test_chronicler_rejects_unregistered_location(tmp_path):
    """强地名后缀未登记 → 事件拒收（v7："外山打斗"被抽成"鬼面跌出秘境"）。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    c = _chron(ws, pid, "鬼面人跌出秘境 | conflict | 苏晚\n")
    report = c.run("正文略", 1, 1)
    assert report.written == 0
    assert report.rejected and "秘境" in report.rejected[0]


def test_chronicler_allows_registered_location(tmp_path):
    """已登记地点（含前缀粘带）→ 正常入库。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    c = _chron(ws, pid, "苏晚在外山被截杀 | conflict | 苏晚\n")
    report = c.run("正文略", 1, 1)
    assert report.written == 1 and not report.rejected


def test_chronicler_gate_realm_out_of_system(tmp_path):
    """境界变更不在体系内 → 丢弃该字段并入 warning（临时修为/编造境界不入 worldstate）。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    c = _chron(ws, pid, "苏晚突破 | growth | 苏晚\n")
    report = c.commit([ExtractedEvent(summary="苏晚突破", kind="growth",
                                      participant_ids=["char:sw"])], 1, 1,
                      state_changes=[("char:sw", {"realm": "渡劫期大能"})])
    assert report.written == 1
    assert any("不在境界体系内" in w for w in report.warnings)
    state = json.loads(ws._abs(f"{pid}/bible/worldstate.json").read_text(encoding="utf-8"))
    assert state["characters"]["char:sw"]["realm"] == "炼气三层"  # 未被污染


def test_chronicler_gate_realm_in_system_passes(tmp_path):
    """体系内境界变更正常生效。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    c = _chron(ws, pid, "苏晚突破 | growth | 苏晚\n")
    report = c.commit([ExtractedEvent(summary="苏晚突破", kind="growth",
                                      participant_ids=["char:sw"])], 1, 1,
                      state_changes=[("char:sw", {"realm": "炼气五层"})])
    state = json.loads(ws._abs(f"{pid}/bible/worldstate.json").read_text(encoding="utf-8"))
    assert state["characters"]["char:sw"]["realm"] == "炼气五层"
    assert report.state_updates.get("char:sw", {}).get("realm") == "炼气五层"


def test_chronicler_near_dup_dedup(tmp_path):
    """换皮重述（相似摘要+共同参与者）→ 去重不入库（v7 双版本/首尾循环）。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    c = _chron(ws, pid, "苏晚在演武场击败赵虎 | conflict | 苏晚\n")
    assert c.run("正文略", 1, 1).written == 1
    c2 = Chronicler(ws, pid, llm=_TextStub("苏晚在演武场打败了赵虎 | conflict | 苏晚\n"))
    report = c2.run("正文略", 1, 2)
    assert report.written == 0
    assert report.rejected and "近似事件去重" in report.rejected[0]
