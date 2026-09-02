"""角色工厂测试（ADR-022，M3q）。

覆盖：
- 需求队列往返（queue_need / load_queue / drain_queue）
- 确定性闸门五类拒收（名字撞名册/境界体系外/宗门不在名册/关系悬空/行为规格条数）
- 合法草卡 → 注册 active + provenance=origin:factory + 关系名→id + first_appear 落章
- 每章配额 ≤2（拍板 2）：超额需求入队待下章
- JIT 补卡降级为告警器：缺名只入队不写卡（追认通道关死，与 P0-B 合围）
- drain_queue 章前消化：成功出队、失败留队

纪律：单元测试绝不真调 LLM（docs/09 §2.1）——provider 用 StubLLM。
"""

from __future__ import annotations

import json

from novelist.core.character_factory import (CharacterNeed, check_card, drain_queue,
                                             load_queue, produce, queue_need)
from novelist.core.orchestrator import _jit_characters
from novelist.storage.checkpoint import Checkpoint
from novelist.storage.workspace import Workspace


def _project(tmp_path, pid="proj-fac"):
    ws = Workspace(root=str(tmp_path))
    ws.create_project(pid)
    Checkpoint(ws).save(pid, {"id": pid, "title": "测试书", "pipeline_state": "正文",
                              "event_seq": 0})
    return ws, pid


def _write(ws, pid, rel, data):
    ws.write_json(ws._abs(f"{pid}/{rel}"), data)


def _seed(ws, pid):
    """含名册（宗门）+ 一名已存在角色——工厂闸门/钩子可达性都要求。"""
    _write(ws, pid, "bible/characters.json", [
        {"id": "char:sw", "name": "苏晚", "gender": "female", "status": "active",
         "aliases": [], "power": {"level": "炼气三层", "faction": "青云宗"},
         "is_protagonist": True},
    ])
    _write(ws, pid, "bible/worldview.json", {
        "name": "青冥界",
        "power_system": {"levels": ["炼气", "筑基", "金丹", "元婴"]},
        "factions": [{"faction": "青云宗", "name": "青云宗"}, {"faction": "外门"}],
    })
    _write(ws, pid, "bible/locations.json", [{"id": "loc:ws", "name": "外山"}])
    _write(ws, pid, "bible/items.json", [])
    _write(ws, pid, "bible/skills.json", [])


def _stub(reply: str):
    from tests.conftest import StubLLM

    return StubLLM(reply)


def _good_card() -> dict:
    """与 _seed 对齐的合法草卡（境界/宗门/钩子/行为规格全部过闸门）。"""
    return {
        "name": "赵管事", "aliases": [], "gender": "male", "age": 42,
        "species": "人族", "core_traits": ["精明", "市侩"],
        "power": {"level": "炼气五层", "faction": "青云宗"},
        "role": "坊市管事", "behavior_rules": ["童叟无欺，明码标价", "见宗门弟子让三分利"],
        "relationships": [{"target": "苏晚", "type": "坊市常客"}],
        "arc": "守着坊市一亩三分地",
    }


def _needs_path(ws, pid):
    return ws._abs(f"{pid}/bible/character_needs_pending.json")


# ---------------------------------------------------------------- 队列往返


def test_need_queue_roundtrip(tmp_path):
    """queue_need → load_queue 保真（含 source/vol/ch/hooks）。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    need = CharacterNeed(role="执法长老", description="需要一名执法长老镇场",
                         realm_hint="筑基", faction_hint="青云宗", source="broadcast",
                         vol=1, ch=3, hooks=[{"to": "苏晚", "rel": "师徒"}])
    queue_need(ws, pid, need)
    got = load_queue(ws, pid)
    assert len(got) == 1
    assert got[0].role == "执法长老" and got[0].source == "broadcast"
    assert got[0].vol == 1 and got[0].ch == 3 and got[0].hooks == [{"to": "苏晚", "rel": "师徒"}]


# ---------------------------------------------------------------- 确定性闸门


def test_gate_rejects_taken_name(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    card = _good_card()
    card["name"] = "苏晚"  # 撞名册
    reasons = check_card(card, ws=ws, project_id=pid)
    assert any("撞已登记名册" in r for r in reasons)


def test_gate_rejects_out_of_system_realm(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    card = _good_card()
    card["power"] = {"level": "渡劫期大能", "faction": "青云宗"}  # 体系外
    reasons = check_card(card, ws=ws, project_id=pid)
    assert any("境界不在体系内" in r for r in reasons)


def test_gate_rejects_unknown_faction(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    card = _good_card()
    card["power"] = {"level": "炼气五层", "faction": "幽冥魔教"}  # 名册外
    reasons = check_card(card, ws=ws, project_id=pid)
    assert any("宗门不在名册" in r for r in reasons)


def test_gate_rejects_dangling_relationship(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    card = _good_card()
    card["relationships"] = [{"target": "不存在的人", "type": "关联"}]  # 悬空钩子
    reasons = check_card(card, ws=ws, project_id=pid)
    assert any("关系指向不存在角色" in r for r in reasons)


def test_gate_rejects_too_few_behavior_rules(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    card = _good_card()
    card["behavior_rules"] = ["只有一条"]  # 最小卡规格要求 2-3 条
    reasons = check_card(card, ws=ws, project_id=pid)
    assert any("行为规格须 2-3 条" in r for r in reasons)


# ---------------------------------------------------------------- 生产管线


def test_produce_valid_card_registers_active(tmp_path):
    """合法草卡：过闸门 → 注册 active，provenance=factory，关系名→id，first_appear 落章。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    need = CharacterNeed(role="坊市管事", description="坊市需要一个管事", vol=1, ch=2)
    report = produce(ws, pid, need, _stub(json.dumps(_good_card(), ensure_ascii=False)),
                     vol=1, ch=2)
    assert report.ok, report.rejections
    card = report.card
    assert card["status"] == "active"
    assert card["provenance"]["origin"] == "factory"
    assert card["provenance"]["channel"] == "manual"
    assert card["relationships"] == [{"target": "char:sw", "type": "坊市常客"}]  # 名→id
    assert card["first_appear"] == {"vol": 1, "ch": 2}
    chars = json.loads(ws._abs(f"{pid}/bible/characters.json").read_text(encoding="utf-8"))
    assert len(chars) == 2 and chars[1]["name"] == "赵管事"
    assert not _needs_path(ws, pid).exists()  # 需求成功消费


def test_produce_llm_garbage_requeues(tmp_path):
    """草卡解析失败 → 不入库，需求留队待下章（不阻断生产主链路）。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    need = CharacterNeed(role="管事", description="需要管事", vol=1, ch=1)
    report = produce(ws, pid, need, _stub("不是 JSON 的废话"), vol=1, ch=1)
    assert not report.ok and report.queued
    needs = load_queue(ws, pid)
    assert len(needs) == 1 and needs[0].source == "manual"
    chars = json.loads(ws._abs(f"{pid}/bible/characters.json").read_text(encoding="utf-8"))
    assert len(chars) == 1  # 无污染


def test_produce_gate_failure_not_registered(tmp_path):
    """闸门拒收（如宗门错名）→ 不注册、不静默入档——原因全程披露在 rejections。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    card = _good_card()
    card["power"] = {"level": "炼气五层", "faction": "幽冥魔教"}
    need = CharacterNeed(role="管事", description="需要管事", vol=1, ch=1)
    report = produce(ws, pid, need, _stub(json.dumps(card, ensure_ascii=False)),
                     vol=1, ch=1)
    assert not report.ok and not report.queued
    assert report.rejections and "宗门不在名册" in report.rejections[0]
    chars = json.loads(ws._abs(f"{pid}/bible/characters.json").read_text(encoding="utf-8"))
    assert len(chars) == 1


# ---------------------------------------------------------------- 配额 ≤2（拍板 2）


def _register_factory_char(ws, pid, name, vol, ch):
    """直接写一张 provenance=factory 的卡，模拟本章已产。"""
    chars = json.loads(ws._abs(f"{pid}/bible/characters.json").read_text(encoding="utf-8"))
    chars.append({"id": f"char:fac{len(chars) + 1}", "name": name, "status": "active",
                  "aliases": [], "gender": "male",
                  "power": {"level": "炼气五层", "faction": "青云宗"},
                  "first_appear": {"vol": vol, "ch": ch},
                  "provenance": {"origin": "factory", "channel": "test"}})
    ws.write_json(ws._abs(f"{pid}/bible/characters.json"), chars)


def test_produce_quota_full_queues(tmp_path):
    """本章工厂产出已达配额 2 → 新需求转队列（拍板 2：每章 ≤2）。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    _register_factory_char(ws, pid, "甲管事", 1, 1)
    _register_factory_char(ws, pid, "乙管事", 1, 1)
    need = CharacterNeed(role="丙管事", description="第三个需求", vol=1, ch=1)
    report = produce(ws, pid, need, _stub(json.dumps(_good_card(), ensure_ascii=False)),
                     vol=1, ch=1)
    assert not report.ok and report.queued
    assert any("配额已满" in r for r in report.rejections)
    assert [n.role for n in load_queue(ws, pid)] == ["丙管事"]


def test_drain_queue_honors_quota_and_leaves_overflow(tmp_path):
    """队列 3 个需求 → drain 只产 2（配额），第 3 个留队下章。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    for i, role in enumerate(["甲管事", "乙管事", "丙管事"], start=1):
        card = _good_card()
        card["name"] = f"新{role}"
        queue_need(ws, pid, CharacterNeed(role=role, description=f"需求{i}", vol=1, ch=1))
    # stub 逐个返回合法草卡（名字各不相同，避开查重）
    class _Seq:
        def __init__(self, names):
            self.names = names
            self.i = 0

        def complete(self, req):
            card = _good_card()
            card["name"] = self.names[self.i]
            self.i += 1
            from novelist.core.llm import LLMResult

            return LLMResult(ok=True, content=json.dumps(card, ensure_ascii=False),
                             finish_reason="stop", blocked=False)

    reports = drain_queue(ws, pid, _Seq(["甲管事", "乙管事", "丙管事"]), vol=1, ch=1)
    assert sum(1 for r in reports if r.ok) == 2
    remain = load_queue(ws, pid)
    assert [n.role for n in remain] == ["丙管事"]  # 第 3 个留队


# ---------------------------------------------------------------- JIT 降级（告警器）


def test_jit_alarm_queues_not_backfills(tmp_path):
    """细纲声明出场但缺卡 → 不再 LLM 补卡（追认关死），缺名转需求队列 source=jit_alarm。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    stub = _stub("[{\"name\": \"\"}]")  # 若有任何补卡调用，会立刻暴露
    gist = "---\n出场人物: [苏晚, 赵管事]\n---\n正文细纲……"
    n = _jit_characters(ws, pid, 1, 1, gist, stub)
    assert n == 1  # 只有赵管事缺卡
    chars = json.loads(ws._abs(f"{pid}/bible/characters.json").read_text(encoding="utf-8"))
    assert len(chars) == 1  # characters.json 未被写
    needs = load_queue(ws, pid)
    assert len(needs) == 1 and needs[0].source == "jit_alarm"
    assert needs[0].vol == 1 and needs[0].ch == 1
    assert "赵管事" in needs[0].description


def test_jit_alarm_no_missing_returns_zero(tmp_path):
    """细纲出场人物全部有卡 → 0 入队、无副作用。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    gist = "---\n出场人物: [苏晚]\n---\n正文细纲……"
    assert _jit_characters(ws, pid, 1, 1, gist, _stub("")) == 0
    assert not _needs_path(ws, pid).exists()
