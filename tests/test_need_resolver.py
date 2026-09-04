"""needs resolver（检索复用优先于工厂造卡，2026-09-04 用户拍板）测试。

覆盖：
- 确定性打分：职能/阵营/境界/描述/关系钩子各分项与门槛
- LLM 从严判定通过 → 复用（不造新卡、不占配额、serves_needs 回链落盘、需求出队）
- LLM 判定拒绝 → 落回工厂生产路径
- 无候选（打分不过门槛）→ 不发起判定调用，直接工厂
- jit_alarm 点名缺卡 → 跳过复用（池内必然没有）
- 复用不占工厂配额：1 复用 + 2 生产同章全部消化

纪律：单元测试绝不真调 LLM（docs/09 §2.1）——provider 用脚本化替身。
"""

from __future__ import annotations

import json

from novelist.core.character_factory import (CharacterNeed, drain_queue, load_queue,
                                             queue_need, score_card_for_need,
                                             try_reuse)
from novelist.storage.checkpoint import Checkpoint
from novelist.storage.workspace import Workspace


def _project(tmp_path, pid="proj-resolver"):
    ws = Workspace(root=str(tmp_path))
    ws.create_project(pid)
    Checkpoint(ws).save(pid, {"id": pid, "title": "测试书", "pipeline_state": "正文",
                              "event_seq": 0})
    return ws, pid


def _seed(ws, pid):
    ws.write_json(ws._abs(f"{pid}/bible/characters.json"), [
        {"id": "char:sw", "name": "苏晚", "gender": "female", "status": "active",
         "aliases": [], "power": {"level": "炼气三层", "faction": "青云宗"},
         "is_protagonist": True},
        {"id": "char:wz", "name": "王长老", "gender": "male", "status": "active",
         "aliases": ["王铁面"], "role": "执法长老",
         "power": {"level": "炼气九层", "faction": "青云宗"},
         "core_traits": ["铁面", "护短"],
         "behavior_rules": ["违规必罚", "护犊子"], "status": "active"},
    ])
    ws.write_json(ws._abs(f"{pid}/bible/worldview.json"), {
        "name": "青冥界",
        "power_system": {"levels": ["炼气", "筑基", "金丹", "元婴"]},
        "factions": [{"faction": "青云宗", "name": "青云宗"}],
    })
    for rel in ("locations.json", "items.json", "skills.json"):
        ws.write_json(ws._abs(f"{pid}/bible/{rel}"), [])


_NEED = CharacterNeed(role="执法长老", description="本章需要一位执法长老坐镇大比",
                      faction_hint="青云宗", realm_hint="炼气", vol=1, ch=1)


class _Judge:
    """脚本化 provider：含「选角导演」的请求返回判定 JSON，其余按序返回草卡 JSON。"""

    def __init__(self, judge_reply: str, card_names: list[str] | None = None):
        self.judge_reply = judge_reply
        self.card_names = card_names or []
        self.i = 0
        self.calls: list[str] = []

    def complete(self, req):
        content = req.messages[-1].content
        self.calls.append(content)
        from novelist.core.llm import LLMResult

        if "选角导演" in content:
            return LLMResult(ok=True, content=self.judge_reply,
                             finish_reason="stop", blocked=False)
        assert self.i < len(self.card_names), "未预期的草卡调用"
        card = _good_card()
        card["name"] = self.card_names[self.i]
        self.i += 1
        return LLMResult(ok=True, content=json.dumps(card, ensure_ascii=False),
                         finish_reason="stop", blocked=False)


def _good_card():
    return {"name": "赵管事", "aliases": [], "gender": "male", "age": 42,
            "species": "人族", "core_traits": ["精明", "市侩"],
            "power": {"level": "炼气五层", "faction": "青云宗"},
            "role": "坊市管事",
            "behavior_rules": ["童叟无欺，明码标价", "见宗门弟子让三分利"],
            "relationships": [{"target": "苏晚", "type": "坊市常客"}],
            "arc": "守着坊市一亩三分地"}


# ---------------------------------------------------------------- 打分

def test_score_role_faction_realm_hooks(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    chars = json.loads(ws._abs(f"{pid}/bible/characters.json").read_text(encoding="utf-8"))
    s, hits = score_card_for_need(chars[1], _NEED)
    assert s >= 5.0  # 职能全覆盖 + 阵营 + 境界
    assert any(h.startswith("职能") for h in hits) and any(h.startswith("阵营") for h in hits)
    s0, _ = score_card_for_need(chars[0], _NEED)
    assert s0 < 4.0   # 主角卡只有阵营+境界（压线分），不应过门槛


def test_score_hook_bonus():
    need = CharacterNeed(role="护法", hooks=[{"to": "王铁面", "rel": "旧部"}])
    card = {"name": "王长老", "aliases": ["王铁面"], "role": "护法",
            "power": {"level": "筑基", "faction": "青云宗"}}
    s, hits = score_card_for_need(card, need)
    assert any(h.startswith("钩子") for h in hits)


# ---------------------------------------------------------------- 复用主路径

def test_reuse_success_no_new_card_no_quota(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    llm = _Judge('{"reuse": "王长老", "reason": "职能与阵营均匹配"}')
    report = try_reuse(ws, pid, _NEED, llm, vol=1, ch=2)
    assert report.ok and report.reused == "王长老"
    chars = json.loads(ws._abs(f"{pid}/bible/characters.json").read_text(encoding="utf-8"))
    assert len(chars) == 2                       # 未造新卡
    assert chars[1]["serves_needs"] == [{"role": _NEED.role, "description": _NEED.description,
                                         "vol": 1, "ch": 2, "source": "manual"}]


def test_reuse_rejected_falls_to_factory(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    queue_need(ws, pid, _NEED)
    llm = _Judge('{"reuse": null, "reason": "境界压不住金丹来宾"}', card_names=["赵管事"])
    reports = drain_queue(ws, pid, llm, vol=1, ch=1)
    assert len(reports) == 1 and reports[0].ok and not reports[0].reused
    assert any("检索复用" in r for r in reports[0].rejections)   # 复用尝试留痕
    chars = json.loads(ws._abs(f"{pid}/bible/characters.json").read_text(encoding="utf-8"))
    assert len(chars) == 3 and chars[-1]["name"] == "赵管事"     # 工厂补位
    assert load_queue(ws, pid) == []                             # 需求出队


def test_no_candidates_skips_judge_call(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    queue_need(ws, pid, CharacterNeed(role="炼丹师", description="需要炼丹师", vol=1, ch=1))
    llm = _Judge('{"reuse": null}', card_names=["赵管事"])
    reports = drain_queue(ws, pid, llm, vol=1, ch=1)
    assert reports[0].ok and not reports[0].reused
    assert len(llm.calls) == 1 and "选角导演" not in llm.calls[0]  # 未发起判定


def test_jit_alarm_skips_reuse(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    need = CharacterNeed(role="执法长老", description="细纲声明出场但 bible 缺卡：李千机",
                         source="jit_alarm", vol=1, ch=1)
    queue_need(ws, pid, need)
    llm = _Judge('{"reuse": "王长老"}', card_names=["赵管事"])
    reports = drain_queue(ws, pid, llm, vol=1, ch=1)
    assert all("选角导演" not in c for c in llm.calls)           # 复用判定未发起
    assert reports[0].ok and not reports[0].reused


def test_reuse_does_not_consume_quota(tmp_path):
    """1 复用 + 2 生产同章全消化（复用不占配额 2）。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    queue_need(ws, pid, _NEED)                                   # 可复用
    for i, role in enumerate(["甲管事", "乙管事"], start=1):      # 走工厂
        queue_need(ws, pid, CharacterNeed(role=role, description=f"需求{i}", vol=1, ch=1))
    llm = _Judge('{"reuse": "王长老", "reason": "匹配"}', card_names=["甲管事", "乙管事"])
    reports = drain_queue(ws, pid, llm, vol=1, ch=1)
    assert sum(1 for r in reports if r.ok) == 3
    assert sum(1 for r in reports if r.reused) == 1
    assert load_queue(ws, pid) == []
