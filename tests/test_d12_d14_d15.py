"""D12/D14/D15 测试：跨切片位置记忆 / 隐藏实力调度松绑 / 开篇元指令。

- D12（人工审查 Q1）：张胖子反复"窜出来"——事件切片 prompt 注入本章已出场记录，
  后续事件登场方式必须变化。
- D14（Q3）：扮猪吃虎主角被每事件 taboo"绝不能露威压"压住逼格——导演纪律松绑
  （禁的是对剧中人物暴露，不禁对读者显从容），正文人物名单透出 hidden_level。
- D15（Q4）：第一章缺背景交代——正文 system prompt 加【开篇任务】块，
  细纲第一章加开场交代纪律。
"""

from __future__ import annotations

from novelist.core.context import build_system_prompt
from novelist.core.director import DIRECTOR_PROMPT
from novelist.core.orchestrator import _event_goal
from novelist.forge.nodes import NodeContext, _chapter_prompt
from novelist.providers.fake import FakeProvider

from test_d13_anchors import _mk_bp


# ---- D12 ----
def test_event_goal_renders_appearance_notes():
    out = _event_goal("目标", "事件文本", 2, 3, "", 300, [], False,
                      appeared_notes=["- 张胖子（第1场）：从食堂后门窜出来吓唬人"])
    assert "已出场记录" in out and "张胖子" in out and "严禁重复" in out


def test_event_goal_no_notes_no_block():
    out = _event_goal("目标", "事件文本", 1, 3, "", 300, [], False)
    assert "已出场记录" not in out


# ---- D14 ----
def test_director_prompt_relaxes_hidden_power():
    assert "刻意隐藏" in DIRECTOR_PROMPT
    assert "暴露真实修为" in DIRECTOR_PROMPT
    assert "狼狈" in DIRECTOR_PROMPT  # 明确禁止写崩


def test_system_prompt_cast_line_shows_hidden_level():
    bible = {"worldview": {"name": "蓝星", "power_system": {"levels": ["练气", "渡劫"]}},
             "characters": [], "style": {}}
    cast = [{"id": "char:a", "name": "李天劫", "is_protagonist": True,
             "power": {"level": "练气三层", "hidden_level": "渡劫圆满"}}]
    out = build_system_prompt(bible, cast, 1, 2)
    assert "实际战力 渡劫圆满" in out
    assert "越级表现是有意设定" in out


# ---- D15 ----
def _bible():
    return {"worldview": {"name": "蓝星", "rules": ["灵气复苏刚一年"],
                          "power_system": {"levels": ["练气", "筑基", "渡劫"]}},
            "characters": [], "style": {}}


def test_first_chapter_has_opening_task():
    cast = [{"id": "char:a", "name": "李天劫", "is_protagonist": True,
             "power": {"level": "练气三层"}}]
    out = build_system_prompt(_bible(), cast, 1, 1)
    assert "【开篇任务】" in out
    assert "世界背景" in out
    assert "练气>筑基>渡劫" in out  # 力量体系注入
    assert "李天劫" in out and "金手指" in out


def test_non_first_chapter_has_no_opening_task():
    out = build_system_prompt(_bible(), [], 1, 2)
    assert "【开篇任务】" not in out


def test_chapter_gist_prompt_has_opening_rule_only_for_ch1(ws_factory):
    ws, pid = ws_factory("proj-d15")
    bp = _mk_bp(ws, pid, _mk_spec_zero(), "一句话（无数字）")
    bp.data["characters"] = [{"id": "char:a", "name": "甲", "gender": "male"}]
    bp.data["volumes"] = [{"vol": 1, "title": "v", "summary": "s"}]
    ctx1 = NodeContext(ws=ws, project_id=pid, bp=bp, provider=FakeProvider(reply=""),
                       pack={}, vol=1, ch=1)
    ctx2 = NodeContext(ws=ws, project_id=pid, bp=bp, provider=FakeProvider(reply=""),
                       pack={}, vol=1, ch=2)
    assert "开场交代" in _chapter_prompt(ctx1)[1]
    assert "开场交代" not in _chapter_prompt(ctx2)[1]


def _mk_spec_zero():
    import json

    from test_m13_forge_f1 import SEED_REPLY

    return _parse_seed_spec_(json.loads(SEED_REPLY) | {"anchors": []})


def _parse_seed_spec_(data: dict):
    import json

    from novelist.forge.seed import _parse_seed_spec

    return _parse_seed_spec(json.dumps(data, ensure_ascii=False))
