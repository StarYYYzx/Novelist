"""P0-A 人物数据补喂测试（M3r）。

覆盖：
- 缺料判定：缺 relationships / behavior_rules 才提案，有料卡跳过
- 确定性闸门：串卡 / 悬空关系 / 自指 / 重复 target / 行为规格条数 / age 越界
- 合法提案 → 写 characters_enrich_pending.json（target 保留名册原样，合并时解析 id）
- apply --allow：relationships/behavior_rules 并集去重、age 只补缺、provenance=enrich
- apply --deny：移除；已有 relationships 的卡不被清空
- 幂等：已在 pending 的卡不重复提案

纪律：单元测试绝不真调 LLM（docs/09 §2.1）——provider 用 StubLLM。
"""

from __future__ import annotations

import json

from novelist.core.character_enrich import (apply_pending, check_proposal, list_pending,
                                            load_pending, needs_enrichment, propose)
from novelist.storage.checkpoint import Checkpoint
from novelist.storage.workspace import Workspace
from tests.conftest import StubLLM


def _project(tmp_path, pid="proj-enr"):
    ws = Workspace(root=str(tmp_path))
    ws.create_project(pid)
    Checkpoint(ws).save(pid, {"id": pid, "title": "测试书", "pipeline_state": "正文",
                              "event_seq": 0})
    return ws, pid


def _write(ws, pid, rel, data):
    ws.write_json(ws._abs(f"{pid}/{rel}"), data)


def _seed(ws, pid):
    """五张卡：两张有料（含已有关系），三张缺料。"""
    _write(ws, pid, "bible/characters.json", [
        {"id": "char:sw", "name": "苏晚", "gender": "female", "age": 18,
         "power": {"level": "炼气三层", "faction": "青云宗"},
         "core_traits": ["飒爽"], "arc": "执剑问道",
         "relationships": [{"target": "char:yelan", "type": "同门"}],
         "behavior_rules": ["剑出必见血"], "is_protagonist": True},
        {"id": "char:yelan", "name": "叶岚", "gender": "male",
         "power": {"level": "炼气三层", "faction": "青云宗"},
         "core_traits": ["稳健"], "arc": "穿越者步步为营"},
        {"id": "char:zhao", "name": "赵虎", "gender": "male",
         "power": {"level": "炼气五层", "faction": "青云宗"},
         "core_traits": ["莽"], "arc": "外门刺头"},
        {"id": "char:qin", "name": "秦霜", "gender": "female",
         "power": {"level": "筑基一层", "faction": "青云宗"},
         "core_traits": ["冷"], "arc": "执法堂新秀"},
        {"id": "char:xitong", "name": "五五开系统", "gender": "unknown",
         "species": "系统", "power": {"level": "?", "faction": "?"},
         "core_traits": [], "arc": ""},
    ])
    _write(ws, pid, "bible/worldview.json", {
        "name": "青冥界",
        "power_system": {"levels": ["炼气", "筑基", "金丹", "元婴"]},
        "factions": [{"faction": "青云宗"}],
    })
    _write(ws, pid, "bible/plot_threads.json", [
        {"id": "thread:t", "status": "active", "desc": "五五开系统的来历"},
    ])
    # 实然前情（叶岚档案）——提案输入的一部分
    p = ws._abs(f"{pid}/memory/character_histories/char:yelan.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "char_id": "char:yelan", "revision": 1,
        "entries": [{"at": {"vol": 1, "ch": 1},
                     "summary": "叶岚魂穿青云宗杂役，觉醒五五开系统"}],
    }, ensure_ascii=False), encoding="utf-8")


def _proposal_reply() -> str:
    return json.dumps({
        "name": "叶岚", "age": 19,
        "relationships": [{"target": "苏晚", "type": "同门相互照应"},
                          {"target": "赵虎", "type": "外门对头"}],
        "behavior_rules": ["不主动暴露穿越者身份", "绑定他人前先确认其心性"],
    }, ensure_ascii=False)


class _PerCardStub:
    """按 prompt 中的目标卡名返回对应提案（模拟真实 LLM 逐卡响应）。"""

    def complete(self, req):
        import re

        from novelist.core.llm import LLMResult

        content = req.messages[-1].content
        m = re.search(r"目标人物卡：· (.+?)[｜|]", content or "")
        name = m.group(1) if m else "未知"
        age = None if name == "五五开系统" else 20
        card = {"name": name, "age": age,
                "relationships": [{"target": "苏晚", "type": f"{name}与苏晚旧识"}],
                "behavior_rules": [f"{name}不动摇本心", "言出必践"]}
        return LLMResult(ok=True, content=json.dumps(card, ensure_ascii=False),
                         finish_reason="stop", blocked=False)


# ---------------------------------------------------------------- 缺料判定


def test_needs_enrichment_only_missing():
    assert needs_enrichment({"name": "a"}) == ["relationships", "behavior_rules"]
    # 空列表/空数组同样视为缺（渲染无料）
    assert needs_enrichment({"name": "a", "relationships": []}) == ["relationships",
                                                                    "behavior_rules"]
    assert needs_enrichment({"name": "a", "relationships": [{}],
                             "behavior_rules": ["x"]}) == []


# ---------------------------------------------------------------- 确定性闸门


def test_gate_rejects_wrong_name(tmp_path):
    """串卡：提案 name ≠ 目标卡 → 直接拒（防提案张冠李戴）。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    card = {"id": "char:yelan", "name": "叶岚"}
    reasons = check_proposal(card, {"name": "苏晚", "relationships": []}, ws=ws, project_id=pid)
    assert reasons and "串卡" in reasons[0]


def test_gate_rejects_dangling_target(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    card = {"id": "char:yelan", "name": "叶岚", "relationships": []}
    reasons = check_proposal(card, {
        "name": "叶岚",
        "relationships": [{"target": "不存在的人", "type": "x"}],
    }, ws=ws, project_id=pid)
    assert any("指向不存在角色" in r for r in reasons)


def test_gate_rejects_self_ref_but_skips_dup(tmp_path):
    """自指拒整卡；重复 target（同批/已有）只跳过该条——不拒整卡（rules/age 仍有效）。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    card = {"id": "char:yelan", "name": "叶岚",
            "relationships": [{"target": "char:sw", "type": "已有"}],
            "behavior_rules": ["a", "b"]}
    reasons = check_proposal(card, {
        "name": "叶岚",
        "relationships": [{"target": "叶岚", "type": "自指"}],
    }, ws=ws, project_id=pid)
    assert any("自指" in r for r in reasons)
    # 只含重复 target（与已有 char:sw 重复 + 同批重复）→ 全部跳过，不拒
    reasons2 = check_proposal(card, {
        "name": "叶岚",
        "relationships": [{"target": "苏晚", "type": "与已有重复"},
                          {"target": "苏晚", "type": "同批重复"}],
        "behavior_rules": ["规则一", "规则二"],
    }, ws=ws, project_id=pid)
    assert reasons2 == []


def test_gate_accepts_valid_proposal(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    card = {"id": "char:yelan", "name": "叶岚", "relationships": [], "age": None}
    reasons = check_proposal(card, json.loads(_proposal_reply()), ws=ws, project_id=pid)
    assert reasons == []


# ---------------------------------------------------------------- 提案 → pending


def test_propose_writes_pending_and_skips_ok_cards(tmp_path):
    """缺料卡进 pending（关系 target 保留名册名，合并时解析 id）；有料卡跳过。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    results = propose(ws, pid, _PerCardStub())
    proposed = [r for r in results if r.get("proposed")]
    proposed_names = {r["name"] for r in proposed}
    assert proposed_names == {"叶岚", "赵虎", "秦霜", "五五开系统"}  # 全部缺料卡
    assert "苏晚" not in proposed_names  # 有料卡跳过
    pending = load_pending(ws, pid)
    assert {p["card_name"] for p in pending} == proposed_names
    # 幂等：再跑不重复
    results2 = propose(ws, pid, _PerCardStub())
    assert all(not r.get("proposed") for r in results2)
    assert len(load_pending(ws, pid)) == 4


# ---------------------------------------------------------------- 人工确认合并


def test_apply_allow_merges_and_keeps_existing(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    # 先手动造一份 pending（模拟已提案 2 张）
    _write(ws, pid, "bible/characters_enrich_pending.json", [
        {"card_id": "char:yelan", "card_name": "叶岚", "missing": ["relationships", "behavior_rules"],
         "age": 19,
         "relationships": [{"target": "苏晚", "type": "同门相互照应"}],
         "behavior_rules": ["不主动暴露穿越者身份", "绑定他人前先确认其心性"]},
        {"card_id": "char:zhao", "card_name": "赵虎", "missing": ["relationships"],
         "age": None,
         "relationships": [{"target": "苏晚", "type": "单恋"}],
         "behavior_rules": []},
        {"card_id": "char:qin", "card_name": "秦霜", "missing": ["behavior_rules"],
         "age": None, "relationships": [], "behavior_rules": []},
    ])
    moved, dropped, remaining = apply_pending(ws, pid, allow={"叶岚", "赵虎"},
                                              deny={"秦霜"})
    assert (moved, dropped, remaining) == (2, 1, 0)
    chars = json.loads(ws._abs(f"{pid}/bible/characters.json").read_text(encoding="utf-8"))
    yelan = next(c for c in chars if c["id"] == "char:yelan")
    assert yelan["relationships"] == [{"target": "char:sw", "type": "同门相互照应"}]
    assert yelan["behavior_rules"] == ["不主动暴露穿越者身份", "绑定他人前先确认其心性"]
    assert yelan["age"] == 19
    assert yelan["provenance"]["origin"] == "enrich"
    zhao = next(c for c in chars if c["id"] == "char:zhao")
    assert zhao["relationships"] == [{"target": "char:sw", "type": "单恋"}]
    assert zhao.get("behavior_rules") in (None, [])
    # 苏晚已有关系未被清空、age 未被动
    su = next(c for c in chars if c["id"] == "char:sw")
    assert su["relationships"] == [{"target": "char:yelan", "type": "同门"}]
    assert su["age"] == 18 and "enrich" not in su.get("provenance", {}).get("origin", "")
    assert load_pending(ws, pid) == []


def test_apply_merges_with_existing_rels_no_dup(tmp_path):
    """卡已有关系时 allow 合并：同 target 不重复、不同 target 追加。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    # 给叶岚造一条已有关系
    _write(ws, pid, "bible/characters.json", [
        {"id": "char:sw", "name": "苏晚", "gender": "female", "age": 18,
         "power": {"level": "炼气三层", "faction": "青云宗"},
         "relationships": [{"target": "char:yelan", "type": "同门"}], "is_protagonist": True},
        {"id": "char:yelan", "name": "叶岚", "gender": "male",
         "power": {"level": "炼气三层", "faction": "青云宗"},
         "relationships": [{"target": "char:sw", "type": "旧关系"}],
         "behavior_rules": ["旧规则"]},
    ])
    _write(ws, pid, "bible/characters_enrich_pending.json", [
        {"card_id": "char:yelan", "card_name": "叶岚",
         "missing": ["relationships", "behavior_rules"], "age": None,
         "relationships": [{"target": "苏晚", "type": "同门相互照应"}],  # 与旧 target 重复
         "behavior_rules": ["旧规则", "新规则一", "新规则二", "新规则三"]},
    ])
    moved, dropped, remaining = apply_pending(ws, pid, allow={"叶岚"})
    assert (moved, remaining) == (1, 0)
    chars = json.loads(ws._abs(f"{pid}/bible/characters.json").read_text(encoding="utf-8"))
    yelan = next(c for c in chars if c["id"] == "char:yelan")
    # 同 target 保留旧关系（不重复追加）；行为规则并集去重、截断 ≤3
    assert yelan["relationships"] == [{"target": "char:sw", "type": "旧关系"}]
    assert yelan["behavior_rules"] == ["旧规则", "新规则一", "新规则二"]