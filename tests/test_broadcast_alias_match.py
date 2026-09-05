"""方案6.3/6.4（质量加固 2026-09-05）：广播别名归一匹配 + 被拒点名收集。

真机实证（proj-cloud5 ch1）：char:love_interest 的 name 被污染成 40 字人设长句，
parse_decision 名字精确匹配失败 → 苏晚晴被当"自造名"拒绝 → 悬空。
"""

from __future__ import annotations

import json

from novelist.core.broadcast import (_norm_name, build_alias_map, parse_decision)


def test_norm_name_strips_annotation():
    assert _norm_name("苏晚晴（青梅竹马，前世主角最愧疚之人）") == "苏晚晴"
    assert _norm_name("苏晚晴") == "苏晚晴"
    assert _norm_name(" 李天劫 ") == "李天劫"


def test_build_alias_map():
    pool = [{"id": "char:li", "name": "李天劫", "aliases": ["劫哥", "小李"]}]
    am = build_alias_map(pool)
    assert am["李天劫"] == "李天劫"
    assert am["劫哥"] == "李天劫" and am["小李"] == "李天劫"


def test_parse_decision_alias_rescue():
    """name 污染池 + 干净点名 → 别名归一表捞回（不再误杀）。"""
    polluted = "苏晚晴（青梅竹马，前世主角最愧疚之人）"
    amap = {"苏晚晴": polluted}
    content = json.dumps({"present": [{"name": "苏晚晴",
                                       "reason_category": "伏笔相关",
                                       "reason": "事件直接点名"}]})
    members, _needs, alarms = parse_decision(content, {polluted}, alias_map=amap)
    assert [m.name for m in members] == [polluted]
    assert any("归一" in a for a in alarms)


def test_parse_decision_contains_fallback():
    """包含兜底：点名是池名的扩展（如带称谓后缀）→ 归一 + 告警。"""
    content = json.dumps({"present": [{"name": "李天劫大哥",
                                       "reason_category": "职能必需",
                                       "reason": "主角在场"}]})
    members, _needs, alarms = parse_decision(content, {"李天劫"})
    assert [m.name for m in members] == ["李天劫"]
    assert any("包含匹配" in a for a in alarms)


def test_parse_decision_collects_rejected():
    content = json.dumps({"present": [{"name": "路人甲",
                                       "reason_category": "职能必需",
                                       "reason": "看热闹"}]})
    rejected: list[str] = []
    members, _needs, alarms = parse_decision(content, {"李天劫"},
                                             rejected_out=rejected)
    assert members == []
    assert rejected == ["路人甲"]
    assert any("不在可及池" in a for a in alarms)


def test_parse_decision_backward_compat():
    """两参调用（旧签名语义）不破：池内精确匹配照常。"""
    content = json.dumps({"present": [{"name": "李天劫",
                                       "reason_category": "职能必需",
                                       "reason": "主角"}]})
    members, needs, alarms = parse_decision(content, {"李天劫"})
    assert [m.name for m in members] == ["李天劫"] and not needs
