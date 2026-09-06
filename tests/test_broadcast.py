"""ADR-021 世界广播选角测试（人物一致性栈第 0 层）。

覆盖（docs/03 ADR-021，v1 默认关闭）：
- 可及池确定性过滤：worldstate dead / unavailable_until>now / bible status dead·unknown 剔除；
- 解析纪律：防造名（不在池点名拒绝 + 告警）、```json 围栏容错、非 JSON 不崩；
- 确定性校验五条硬约束中的可测部分：细纲声明补回 / 文本命中补回 / 池外剔除 / 上限裁剪；
- 落盘：save_casting → memory/castings/v{vol}-c{ch}-e{idx}.json（含理由与告警留痕）；
- 缺人需求入队：needs → bible/character_needs_pending.json（source=broadcast，ADR-022 通道）；
- 失败纪律：provider 挂/blocked/解析彻底失败 → None（调用方静默回退确定性选角）；
- orchestrator 集成：broadcast_casting=True 名单驱动 cast（counters 回传 + castings 落盘）、
  broadcast 输出垃圾 → 静默降级确定性路径（broadcasts_built=0 但生成不受阻）。

纪律：单元测试绝不真调 LLM（docs/09 §2.1）——provider 用 StubLLM / FakeProvider。
"""

from __future__ import annotations

import json

from tests.conftest import StubLLM

from novelist.core import broadcast as bc
from novelist.core.broadcast import (available_pool, broadcast_cast, parse_decision,
                                     save_casting, validate_names)
from novelist.core.character_factory import load_queue
from novelist.providers.fake import FakeProvider


def _stub(reply: str) -> StubLLM:
    return StubLLM(reply)


# ---------------------------------------------------------------- 可及池过滤（零模型调用）


def _chars_5() -> list[dict]:
    """5 张卡：2 active + 1 worldstate dead + 1 闭关中 + 1 bible status=dead。"""
    return [
        {"id": "char:yelan", "name": "叶岚", "gender": "male", "status": "active",
         "power": {"level": "炼气三层", "faction": "青云宗"}, "core_traits": ["冷静"]},
        {"id": "char:sw", "name": "苏晚", "gender": "female", "status": "active",
         "power": {"level": "炼气四层", "faction": "青云宗"}, "core_traits": ["刚烈"]},
        {"id": "char:wang", "name": "王长老", "gender": "male", "status": "active",
         "power": {"level": "筑基", "faction": "青云宗"}, "core_traits": []},
        {"id": "char:zhao", "name": "赵长老", "gender": "male", "status": "active",
         "power": {"level": "筑基", "faction": "青云宗"}, "core_traits": []},
        {"id": "char:li", "name": "李长老", "gender": "male", "status": "dead",
         "power": {"level": "筑基", "faction": "青云宗"}, "core_traits": []},
    ]


def test_available_pool_filters_dead_and_unavailable():
    """worldstate 实然 dead / unavailable_until>now 剔除；已解除(≤now)放行；bible status=dead 兜底。"""
    wst = {"time": {"now": 100},
           "characters": {
               "char:yelan": {"dead": True},                 # 实然已死 → 剔除
               "char:sw": {"unavailable_until": 200},        # 闭关到 200 > now 100 → 剔除
               "char:wang": {"unavailable_until": 50},       # 已解除 → 放行
               # char:zhao 无 worldstate 记录 → 走 bible status
               # char:li  bible status=dead → 剔除
           }}
    names = {c["name"] for c in available_pool(_chars_5(), wst)}
    # 王长老（已解除）与赵长老（无 ws 记录，bible active 放行）可及；其余剔除
    assert names == {"王长老", "赵长老"}


def test_available_pool_no_worldstate_uses_bible_status():
    """无 worldstate → bible status ∈ {dead, unknown} 剔除，其余全进池。"""
    chars = _chars_5()
    # 把 char:zhao 标记 unknown（forge 占位）
    chars[3]["status"] = "unknown"
    names = {c["name"] for c in available_pool(chars, None)}
    assert names == {"叶岚", "苏晚", "王长老"}


def test_available_pool_skips_malformed():
    """无 id / 非 dict 条目跳过（load_characters 已过滤，防御性）。"""
    pool = available_pool([{"name": "没id的人"}, "裸字符串", {"id": "char:a", "name": "甲"}], None)
    assert [c["name"] for c in pool] == ["甲"]


# ---------------------------------------------------------------- 解析（防造名 / 容错）


def test_parse_decision_rejects_invented_name():
    """不在池的点名（自造名/已死）→ 拒绝 + 告警；池内点名保留。"""
    pool = {"叶岚", "苏晚"}
    members, needs, alarms = parse_decision(json.dumps({
        "present": [{"name": "叶岚", "reason_category": "职能必需", "reason": "事件主角"},
                    {"name": "上官屠神", "reason_category": "动机主动", "reason": "自造名"}],
        "needs": [],
    }, ensure_ascii=False), pool)
    assert [m.name for m in members] == ["叶岚"]
    assert any("上官屠神" in a and "不在可及池" in a for a in alarms)


def test_parse_decision_json_fence_block():
    """模型偶发把 JSON 包在 ```json 围栏里 → 剥围栏解析成功。"""
    content = '```json\n{"present": [{"name": "苏晚", "reason_category": "关系牵引", "reason": ""}], "needs": []}\n```'
    members, needs, alarms = parse_decision(content, {"苏晚"})
    assert [m.name for m in members] == ["苏晚"]
    assert not alarms


def test_parse_decision_needs_only_alarm():
    """只有缺人需求无名单 → 告警提示，needs 正常解析（不阻断入队）。"""
    content = json.dumps({
        "present": [],
        "needs": [{"role": "执法长老", "realm_hint": "筑基", "relation_hook": "苏晚",
                   "why_existing_fail": "池内无长老"}],
    }, ensure_ascii=False)
    members, needs, alarms = parse_decision(content, {"叶岚", "苏晚"})
    assert not members and len(needs) == 1
    assert needs[0].role == "执法长老" and needs[0].hooks == [{"to": "苏晚", "rel": "待定"}]
    assert any("只给了缺人需求" in a for a in alarms)


def test_parse_decision_garbage_not_crash():
    """非 JSON 且无围栏 → 空结果 + 告警（广播调用方据此返回 None 降级）。"""
    members, needs, alarms = parse_decision("我要写死那个长老", {"叶岚"})
    assert not members and not needs
    assert alarms and "非 JSON" in alarms[0]


# ---------------------------------------------------------------- F7：不在场点名（注册角色 / 真自造名两级）


def test_parse_decision_off_scene_registered_allowed():
    """F7 二级：注册角色不在可及池（闭关/远在别处）→ 按【不在场点名】保留，不拒不拉黑。

    `names` 不含（物理在场者），`off_scene_names` 含；有告警但 `rejected_out` 不收。
    """
    pool = {"叶岚"}
    rejected: list[str] = []
    members, needs, alarms = parse_decision(json.dumps({
        "present": [{"name": "叶岚", "reason_category": "职能必需", "reason": "事件主角"},
                    {"name": "林清月", "reason_category": "动机主动",
                     "reason": "闭关中仍遥闻叶岚之名"}],
        "needs": [],
    }, ensure_ascii=False), pool, registered={"林清月"}, rejected_out=rejected)
    assert [m.name for m in members if not m.off_scene] == ["叶岚"]
    assert [m.name for m in members if m.off_scene] == ["林清月"]
    assert any("不在场点名" in a for a in alarms)
    assert rejected == []     # 注册角色不再被当作自造名拉黑


def test_parse_decision_off_scene_not_registered_rejected():
    """F7 一级：真自造名（池+bible 双查无）仍拒绝 + 告警 + 拉黑（回归）。"""
    pool = {"叶岚"}
    rejected: list[str] = []
    members, needs, alarms = parse_decision(json.dumps({
        "present": [{"name": "叶岚", "reason_category": "职能必需", "reason": "事件主角"},
                    {"name": "上官屠神", "reason_category": "动机主动", "reason": "自造名"}],
        "needs": [],
    }, ensure_ascii=False), pool, registered={"林清月"}, rejected_out=rejected)
    assert [m.name for m in members] == ["叶岚"]
    assert rejected == ["上官屠神"]
    assert any("上官屠神" in a and "已拒绝" in a for a in alarms)


def test_broadcast_cast_off_scene_registered_not_in_physical_cast(ws_factory, write_json):
    """F7 集成：扬名播报点名闭关中的注册角色 → 保留为不在场引用，不进物理 cast。

    dec.names（驱动 N3 调度/正文 cast）= 物理在场者；off_scene_names = 引用者；
    落盘 casting 带 off_scene=true 且不进入正文禁令（rejected 空）。
    """
    ws, pid = ws_factory()
    write_json(ws, pid, "bible/characters.json", [
        {"id": "char:yelan", "name": "叶岚", "gender": "male", "status": "active",
         "power": {"level": "炼气三层", "faction": "青云宗"}},
        {"id": "char:lin", "name": "林清月", "gender": "female", "status": "active",
         "power": {"level": "筑基", "faction": "青云宗"},
         "relationships": [{"target": "char:yelan", "type": "青梅竹马"}]},
    ])
    # 林清月闭关到 day 200 > now 100 → 不在可及池，但仍是注册角色
    wst = {"time": {"now": 100},
           "characters": {"char:lin": {"unavailable_until": 200}}}
    reply = json.dumps({
        "present": [
            {"name": "叶岚", "reason_category": "职能必需", "reason": "扬名主角"},
            {"name": "林清月", "reason_category": "动机主动",
             "reason": "闭关仍遥闻叶岚之名，心绪震动"},
        ],
        "needs": [],
    }, ensure_ascii=False)
    dec = broadcast_cast(ws, pid, _stub(reply), vol=1, ch=3, idx=0,
                         ev_text="叶岚一夜扬名，消息传遍各大宗门，连闭关中的林清月都听闻",
                         declared=["叶岚"], text_hits=["叶岚"],
                         worldstate=wst)
    assert dec is not None
    assert set(dec.names) == {"叶岚"}                 # 物理在场只有主角
    assert set(dec.off_scene_names) == {"林清月"}     # 闭关者仅作引用
    assert dec.rejected == []                          # 不拉黑
    assert any("不在场点名" in a for a in dec.alarms)
    data = json.loads(ws._abs(f"{pid}/memory/castings/v1-c3-e0.json")
                      .read_text(encoding="utf-8"))
    present = {m["name"]: m["off_scene"] for m in data["present"]}
    assert present == {"叶岚": False, "林清月": True}  # 落盘区分物理/不在场


def test_available_pool_keeps_registered_out_of_pool():
    """F7 前置：闭关/失踪者确被排除出可及池（在池才真在场，不在池才触发不在场引用）。"""
    wst = {"time": {"now": 100}, "characters": {"char:lin": {"unavailable_until": 200}}}
    names = {c["name"] for c in available_pool([
        {"id": "char:yelan", "name": "叶岚", "status": "active"},
        {"id": "char:lin", "name": "林清月", "status": "active"},
    ], wst)}
    assert names == {"叶岚"}


# ---------------------------------------------------------------- 确定性校验


def test_validate_names_reinforces_declared():
    """广播遗漏细纲声明 → 强制保留 + 告警（规划层意图不可被删）。"""
    pool = ["叶岚", "苏晚", "王长老"]
    final, alarms = validate_names(["叶岚"], declared=["王长老"], pool=pool, text_hits=[])
    assert set(final) == {"叶岚", "王长老"}
    assert any("遗漏细纲声明" in a for a in alarms)


def test_validate_names_removes_off_pool():
    """名单含池外角色（不可出场者）→ 剔除 + 告警。"""
    final, alarms = validate_names(["叶岚", "苏晚", "李长老"],
                                   declared=[], pool=["叶岚", "苏晚"], text_hits=[])
    assert set(final) == {"叶岚", "苏晚"}
    assert any("池外角色" in a for a in alarms)


def test_validate_names_adds_text_hits():
    """事件文本字面命中必须涵盖 → 缺则补（防广播漏读事件正文）。"""
    final, alarms = validate_names(["叶岚"], declared=[],
                                   pool=["叶岚", "苏晚", "王长老"], text_hits=["王长老"])
    assert set(final) == {"叶岚", "王长老"}
    assert any("文本命中" in a for a in alarms)


def test_validate_names_caps_at_six():
    """超上限裁剪：细纲声明与文本命中优先保留，其余按序截断 + 告警。"""
    pool = [f"p{i}" for i in range(1, 9)]
    final, alarms = validate_names(["p1", "p2", "p3", "p4", "p5"],
                                   declared=["p7", "p8"], pool=pool, text_hits=["p6"])
    assert len(final) == bc.CAST_CAP == 6
    kept = set(final)
    assert {"p6", "p7", "p8"} <= kept            # 命中 + 声明全保
    assert any("超上限" in a for a in alarms)


# ---------------------------------------------------------------- B2：池行关系锚 / B4：封闭场景（ADR-021 v2）


def test_pool_line_carries_title_and_relation_anchor():
    """B2 池行补可溯源字段：aliases（职务/称谓）+ relationships 关系锚（id→名回查）。

    e3 预演报"青云子=掌门"在输入里无据可循——v2 后"掌门"（aliases）与关系
    （如"对墨无极：师祖与掌门"）都进池行，模型不必推断撞对。
    """
    card = {"id": "char:qing", "name": "青云子", "aliases": ["掌门", "青云掌门"],
            "power": {"level": "元婴后期", "faction": "青云宗"},
            "core_traits": ["威严", "务实"],
            "relationships": [{"target": "char:mo", "type": "师祖与掌门，重大决策请示老祖"}]}
    names = {"char:qing": "青云子", "char:mo": "墨无极"}
    line = bc._one_line(card, names)
    assert "掌门" in line                    # 职务称谓进池行
    assert "与墨无极" in line                # 关系锚 target id 回查成人名
    assert "师祖与掌门" in line[:36] or "师祖与掌门" in line  # type 保留（未超 12 字截断线）
    # 长 type 截断，池行不无限膨胀
    card2 = {"id": "char:a", "name": "甲", "aliases": [],
             "power": {}, "core_traits": [],
             "relationships": [{"target": "char:b", "type": "这是一条超过十二个字的超长关系描述"}]}
    line2 = bc._one_line(card2, {"char:b": "乙"})
    assert "与乙（这是一条超过十二" in line2 and "…" in line2


def test_build_pool_block_lines_have_relation_anchors():
    """B2 build_pool_block：每行带关系锚（池行从 ~20 字涨到 ~40，仍在预算内）。"""
    pool = [
        {"id": "char:yelan", "name": "叶岚", "aliases": ["叶哥"],
         "power": {"level": "炼气三层", "faction": "青云宗"}, "core_traits": ["冷静"],
         "relationships": [{"target": "char:sw", "type": "青梅竹马"}]},
        {"id": "char:sw", "name": "苏晚", "aliases": ["苏师姐"],
         "power": {"level": "炼气四层", "faction": "青云宗"}, "core_traits": ["刚烈"],
         "relationships": []},
    ]
    block = bc.build_pool_block(pool, {"char:yelan": "叶岚", "char:sw": "苏晚"})
    assert "与苏晚（青梅竹马）" in block
    assert "苏师姐" in block
    assert block.count("\n") == 1


def test_is_closed_scene_detects_private_events():
    """B4 确定性判定：私密/单独/密室类事件 → 封闭；公开场合 → 开放。"""
    assert bc.is_closed_scene("墨无极在密室单独召见叶岚，屏退左右") is True
    assert bc.is_closed_scene("", "上一事件末，两人在寝殿夜话") is True
    assert bc.is_closed_scene("宗门广场大比，数千弟子围观") is False
    assert bc.is_closed_scene("叶岚下山采购药材，途经坊市") is False


def test_validate_names_custom_cap():
    """B4 校验 cap 参数化：封闭场景 cap=3 时超出的加戏者被裁。"""
    pool = [f"p{i}" for i in range(1, 7)]
    final, alarms = validate_names(["p1", "p2", "p3", "p4"],
                                   declared=["p5"], pool=pool, text_hits=["p6"],
                                   cap=bc.CLOSED_CAP)
    assert len(final) == bc.CLOSED_CAP == 3
    assert {"p5", "p6"} <= set(final)       # 声明 + 文本命中仍优先保留
    assert any("超上限" in a for a in alarms)


def test_broadcast_cast_closed_scene_auto(ws_factory, write_json):
    """B4 集成：事件文本含"密室单独召见" → 自动判封闭。

    模型若按关系牵引加戏（好友/宿敌）→ 确定性拒绝；细纲声明（传召双方）保留。
    落盘 closed_scene=true 留痕。
    """
    ws, pid = ws_factory()
    write_json(ws, pid, "bible/characters.json", [
        {"id": "char:yelan", "name": "叶岚", "gender": "male", "status": "active",
         "power": {"level": "炼气三层", "faction": "青云宗"}},
        {"id": "char:mo", "name": "墨无极", "gender": "male", "status": "active",
         "power": {"level": "化神后期", "faction": "青云宗"}},
        {"id": "char:yun", "name": "云清瑶", "gender": "female", "status": "active",
         "power": {"level": "筑基中期", "faction": "青云宗"}},
        {"id": "char:xue", "name": "雪见", "gender": "female", "status": "active",
         "power": {"level": "金丹后期", "faction": "青云宗"}},
    ])
    reply = json.dumps({
        "present": [
            {"name": "叶岚", "reason_category": "职能必需", "reason": "被召见者"},
            {"name": "云清瑶", "reason_category": "关系牵引", "reason": "叶岚的绑定对象，理应陪护"},
            {"name": "雪见", "reason_category": "动机主动", "reason": "闻讯赶来"},
        ],
        "needs": [],
    }, ensure_ascii=False)
    dec = broadcast_cast(ws, pid, _stub(reply), vol=1, ch=6, idx=0,
                         ev_text="墨无极在密室单独召见叶岚，屏退左右，问起残玉来历",
                         declared=["墨无极", "叶岚"], text_hits=["墨无极", "叶岚"])
    assert dec is not None
    assert set(dec.names) == {"墨无极", "叶岚"}    # 关系牵引（云清瑶）/ 动机主动（雪见）被拒
    assert any("封闭场景拒绝" in a for a in dec.alarms)
    assert len(dec.names) <= bc.CLOSED_CAP
    # 落盘留痕
    data = json.loads(ws._abs(f"{pid}/memory/castings/v1-c6-e0.json")
                      .read_text(encoding="utf-8"))
    assert data["closed_scene"] is True


def test_broadcast_cast_open_scene_allows_relation_pull(ws_factory, write_json):
    """开放场景不受影响：关系牵引成员保留（回归——不能把公开场合也收死）。"""
    ws, pid = ws_factory()
    write_json(ws, pid, "bible/characters.json", [
        {"id": "char:yelan", "name": "叶岚", "gender": "male", "status": "active",
         "power": {"level": "炼气三层", "faction": "青云宗"}},
        {"id": "char:yun", "name": "云清瑶", "gender": "female", "status": "active",
         "power": {"level": "筑基中期", "faction": "青云宗"}},
    ])
    reply = json.dumps({
        "present": [
            {"name": "叶岚", "reason_category": "职能必需", "reason": "事件主角"},
            {"name": "云清瑶", "reason_category": "关系牵引", "reason": "与叶岚并肩同行"},
        ],
        "needs": [],
    }, ensure_ascii=False)
    dec = broadcast_cast(ws, pid, _stub(reply), vol=1, ch=7, idx=0,
                         ev_text="宗门坊市，叶岚与云清瑶并肩挑选法器",
                         declared=["叶岚"], text_hits=["叶岚", "云清瑶"])
    assert dec is not None and set(dec.names) == {"叶岚", "云清瑶"}
    assert not any("封闭场景" in a for a in dec.alarms)


# ---------------------------------------------------------------- 落盘与需求入队


def test_save_casting_writes_file(ws_factory):
    """广播决定落盘 memory/castings/：名单/理由/needs/告警/raw 全留痕（可审"他为什么在"）。"""
    ws, pid = ws_factory()
    dec = bc.CastDecision(
        members=[bc.CastMember(name="叶岚", reason_category="职能必需", reason="事件主角")],
        needs=[bc.CastNeed(role="执法长老", description="广播缺人需求：执法长老")],
        raw='{"present": [...]}', alarms=["测试告警"])
    save_casting(ws, pid, 1, 1, 2, dec)
    p = ws._abs(f"{pid}/memory/castings/v1-c1-e2.json")
    assert p.exists()
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["present"][0]["name"] == "叶岚"
    assert data["needs"][0]["role"] == "执法长老"
    assert data["alarms"] == ["测试告警"] and data["raw"].startswith("{")


def test_broadcast_cast_success_and_persist(ws_factory, write_json):
    """全链路成功：可及池 → LLM(JSON) → 校验 → 落盘 → 返回 decision.names。"""
    ws, pid = ws_factory()
    write_json(ws, pid, "bible/characters.json", [
        {"id": "char:yelan", "name": "叶岚", "gender": "male", "status": "active",
         "power": {"level": "炼气三层", "faction": "青云宗"}},
        {"id": "char:sw", "name": "苏晚", "gender": "female", "status": "active",
         "power": {"level": "炼气四层", "faction": "青云宗"}},
        {"id": "char:zhao", "name": "赵长老", "gender": "male", "status": "dead",
         "power": {"level": "筑基", "faction": "青云宗"}},
    ])
    reply = json.dumps({
        "present": [{"name": "叶岚", "reason_category": "职能必需", "reason": "事件主角"},
                    {"name": "苏晚", "reason_category": "关系牵引", "reason": "与叶岚有旧情"}],
        "needs": [],
    }, ensure_ascii=False)
    dec = broadcast_cast(ws, pid, _stub(reply), vol=1, ch=1, idx=0,
                         ev_text="叶岚在宗门广场遇袭，苏晚出手相救",
                         declared=[], text_hits=["叶岚"])
    assert dec is not None and set(dec.names) == {"叶岚", "苏晚"}
    assert not dec.alarms
    # 落盘
    p = ws._abs(f"{pid}/memory/castings/v1-c1-e0.json")
    assert p.exists()
    data = json.loads(p.read_text(encoding="utf-8"))
    assert {m["name"] for m in data["present"]} == {"叶岚", "苏晚"}
    # 池外者（赵长老 dead）若被点名必被拒——此处未点名，无告警


def test_broadcast_cast_needs_enqueued_to_factory(ws_factory, write_json):
    """needs → character_factory 需求队列（source=broadcast，ADR-022 章前 drain 消化）。"""
    ws, pid = ws_factory()
    write_json(ws, pid, "bible/characters.json", [
        {"id": "char:yelan", "name": "叶岚", "gender": "male", "status": "active",
         "power": {"level": "炼气三层", "faction": "青云宗"}},
    ])
    reply = json.dumps({
        "present": [],
        "needs": [{"role": "执法长老", "realm_hint": "筑基", "faction_hint": "青云宗",
                   "relation_hook": "叶岚", "why_existing_fail": "池内全是炼气弟子"}],
    }, ensure_ascii=False)
    dec = broadcast_cast(ws, pid, _stub(reply), vol=2, ch=3, idx=1,
                         ev_text="宗门需要一名执法长老主持公道", declared=[],
                         text_hits=[])
    assert dec is not None and dec.needs and not dec.names
    needs = load_queue(ws, pid)
    assert len(needs) == 1
    assert needs[0].source == "broadcast" and needs[0].vol == 2 and needs[0].ch == 3
    assert needs[0].role == "执法长老" and needs[0].hooks == [{"to": "叶岚", "rel": "待定"}]


def test_broadcast_cast_failure_returns_none(ws_factory, write_json):
    """失败纪律：provider 挂(异常)/blocked/解析彻底失败/无 provider → None，不抛不落盘。"""
    ws, pid = ws_factory()
    write_json(ws, pid, "bible/characters.json", [
        {"id": "char:yelan", "name": "叶岚", "gender": "male", "status": "active",
         "power": {"level": "炼气三层", "faction": "青云宗"}},
    ])

    class _Boom:
        def complete(self, req):
            raise RuntimeError("provider down")

    assert broadcast_cast(ws, pid, _Boom(), vol=1, ch=1, idx=0,
                          ev_text="x", declared=[]) is None
    assert broadcast_cast(ws, pid, FakeProvider(blocked=True), vol=1, ch=1, idx=0,
                          ev_text="x", declared=[]) is None
    assert broadcast_cast(ws, pid, _stub("这不是 JSON"), vol=1, ch=1, idx=0,
                          ev_text="x", declared=[]) is None
    assert broadcast_cast(ws, pid, None, vol=1, ch=1, idx=0,
                          ev_text="x", declared=[]) is None
    assert not ws._abs(f"{pid}/memory/castings").exists()  # 无落盘残留


def _seq_complete(replies: list[str]):
    """定序 provider：依次返回固定 content；耗尽后返回空。返回 (provider, calls)。"""
    from novelist.core.llm import LLMResult

    calls: list[str] = []

    class _Seq:
        def __init__(self):
            self.replies = list(replies)

        def complete(self, req):
            calls.append(req.messages[-1].content if req.messages else "")
            if not self.replies:
                return LLMResult(ok=True, content="", finish_reason="stop", blocked=False)
            return LLMResult(ok=True, content=self.replies.pop(0),
                             finish_reason="stop", blocked=False)

    return _Seq(), calls


def test_broadcast_cast_retries_empty_cast_converges(ws_factory, write_json):
    """思考抖动重试：先返回空 cast({present:[]})，再给有效 cast → 重试收敛采用后者。

    provider 调用数 = 2（空那次被丢弃），最终名单不含空结果，落盘仅一份好决定。
    """
    ws, pid = ws_factory()
    write_json(ws, pid, "bible/characters.json", [
        {"id": "char:yelan", "name": "叶岚", "gender": "male", "status": "active",
         "power": {"level": "炼气三层", "faction": "青云宗"}},
    ])
    good = _broadcast_reply(["叶岚"])
    llm, calls = _seq_complete(['{"present":[],"needs":[]}', good])
    dec = broadcast_cast(ws, pid, llm, vol=1, ch=1, idx=0,
                         ev_text="叶岚在宗门广场", declared=[], text_hits=["叶岚"])
    assert dec is not None and set(dec.names) == {"叶岚"}
    assert len(calls) == 2, f"空结果应触发重试，实际 {len(calls)} 次"
    assert ws._abs(f"{pid}/memory/castings/v1-c1-e0.json").exists()


def test_broadcast_cast_retries_exhausted_returns_none(ws_factory, write_json):
    """思考抖动重试尽：始终返回空 cast → 3 次后降级 None，不落盘不崩。"""
    ws, pid = ws_factory()
    write_json(ws, pid, "bible/characters.json", [
        {"id": "char:yelan", "name": "叶岚", "gender": "male", "status": "active",
         "power": {"level": "炼气三层", "faction": "青云宗"}},
    ])
    llm, calls = _seq_complete(['{"present":[],"needs":[]}'] * 3)
    dec = broadcast_cast(ws, pid, llm, vol=1, ch=1, idx=0,
                         ev_text="叶岚在宗门广场", declared=[], text_hits=["叶岚"])
    assert dec is None
    assert len(calls) == 3
    assert not ws._abs(f"{pid}/memory/castings").exists()


# ---------------------------------------------------------------- orchestrator 集成


def _seq_llm(replies):
    """队列式 LLM 替身（与 test_m19 同款）：逐次弹出固定回复。"""
    from novelist.core.llm import LLMResult

    class _Seq:
        def __init__(self):
            self.replies = list(replies)
            self.calls: list[str] = []

        def complete(self, req):
            self.calls.append(req.messages[-1].content if req.messages else "")
            if not self.replies:
                return LLMResult(ok=True, content="", finish_reason="stop", blocked=False)
            return LLMResult(ok=True, content=self.replies.pop(0),
                             finish_reason="stop", blocked=False)

    return _Seq()


def _seed_two_chars(ws, pid, write_json):
    write_json(ws, pid, "bible/characters.json", [
        {"id": "char:yelan", "name": "叶岚", "gender": "male", "status": "active",
         "power": {"level": "炼气三层", "faction": "青云宗"},
         "core_traits": ["冷静", "心机深", "扮猪吃虎"],
         "arc": "从被轻视的弃徒到宗门中坚",
         "relationships": [{"target": "char:sw", "type": "青梅竹马"}],
         "aliases": ["叶师兄", "岚哥"],
         "first_appear": {"vol": 1, "ch": 1}, "is_protagonist": True},
        {"id": "char:sw", "name": "苏晚", "gender": "female", "status": "active",
         "power": {"level": "炼气四层", "faction": "青云宗"},
         "core_traits": ["刚烈", "护短"],
         "arc": "为宗门存亡扛下污名",
         "aliases": ["苏师姐"],
         "first_appear": {"vol": 1, "ch": 1}},
    ])


def _write_gist_two_events(ws, pid):
    gist = ws.outline_chapter_path(pid, 1, 1)
    gist.parent.mkdir(parents=True, exist_ok=True)
    gist.write_text(
        "---\nvol: 1\nch: 1\ntitle: 弃徒\n"
        "key_events: [叶岚下山拾玉, 叶岚与苏晚月下夜谈]\n---\n\n正文要点",
        encoding="utf-8")


def _event_reply_blocks() -> tuple[list[str], list[str], str]:
    """两个事件的 (e1 块, e2 块, 章末拟题)——m19 骨架：每事件 5 次 LLM。"""
    e1 = [
        "叶岚 | 冷静、心机深 | 话少先观察 | 不得自曝穿越\n"
        "苏晚 | 刚烈、护短 | 暗中跟随 | 不得示弱",                       # 调度单
        "叶岚下了山，在山道旁拾起半枚焦黑玉佩，掌心发烫，他垂眼收进怀里。",  # 正文
        "ok",                                                            # 审校（无 block）
        "拾玉 | discovery | 叶岚",                                      # 编纂
        "叶岚 | 警觉 | 这玉佩来得蹊跷 | 苏晚：无\n"                       # 视角
        "苏晚 | 好奇 | 想追问却忍住了 | 叶岚：更神秘了",
    ]
    e2 = [
        "叶岚 | 冷静、心机深 | 试探苏晚来历 | 不得说出前世记忆\n"
        "苏晚 | 刚烈、护短 | 咬唇不语 | 不得泄露宗门密辛",                # 调度单
        "入夜，两人在月下相对。叶岚试探着问起玉佩，身为师妹的苏晚别开脸，只说不知。",  # 正文
        "ok",                                                            # 审校
        "夜谈 | plot | 叶岚、苏晚",                                     # 编纂
        "叶岚 | 审慎 | 苏晚有所隐瞒 | 苏晚：防备\n"                       # 视角
        "苏晚 | 挣扎 | 玉佩涉及宗门旧案，不能说 | 叶岚：愧疚",
    ]
    return e1, e2, "月下试探"


def _broadcast_reply(names: list[str]) -> str:
    present = []
    for i, n in enumerate(names):
        present.append({"name": n, "reason_category": "职能必需",
                        "reason": "事件相关" if i == 0 else "关系牵引"})
    return json.dumps({"present": present, "needs": []}, ensure_ascii=False)


def _produce(ws, pid, llm, *, broadcast_casting: bool):
    from novelist.core.orchestrator import produce_chapter
    from novelist.core.session import SessionInfo

    return produce_chapter(
        ws, pid, 1, 1, llm, prefer_direct=True,
        inject_bible=False, event_loop=True, commit_chapter_event=False,
        knowledge_llm=False, event_polish=False, supplement_settings=False,
        session=SessionInfo(project_id=pid, agent="t"),
        defer_title=True, cast_injection=True,
        character_direction=True, perspective_memory=True,
        broadcast_casting=broadcast_casting)


def test_orchestrator_broadcast_drives_cast(ws_factory, write_json):
    """broadcast_casting=True：每事件广播一次 → 名单驱动 match_cast → counters 回传 + castings 落盘。

    每事件 6 次 LLM：广播 → 调度单 → 正文 → 审校 → 编纂 → 视角；2 事件 + 章末拟题 = 13 次。
    """
    ws, pid = ws_factory()
    _seed_two_chars(ws, pid, write_json)
    _write_gist_two_events(ws, pid)
    e1, e2, title = _event_reply_blocks()
    replies = ([_broadcast_reply(["叶岚", "苏晚"])] + e1
               + [_broadcast_reply(["叶岚", "苏晚"])] + e2 + [title])
    llm = _seq_llm(replies)
    res = _produce(ws, pid, llm, broadcast_casting=True)
    assert res.ok, res.result
    assert res.broadcasts_built == 2, \
        f"广播计数必须回传，实际 {res.broadcasts_built}"
    # castings 落盘：2 事件 2 份，名单覆盖文本命中者（叶岚）
    cdir = ws._abs(f"{pid}/memory/castings")
    files = sorted(cdir.glob("v1-c1-e*.json"))
    assert len(files) == 2, f"应有 2 份广播决定，实际 {len(files)}"
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        assert "叶岚" in {m["name"] for m in data["present"]}
    # 广播名单应进正文 prompt 注入面（cast 卡渲染）
    joined = "\n".join(llm.calls)
    assert "叶岚｜男｜青云宗" in joined or "叶岚｜" in joined


def test_orchestrator_broadcast_garbage_falls_back(ws_factory, write_json):
    """广播输出垃圾 → 静默降级确定性选角：broadcasts_built=0、无落盘、生成不受阻。"""
    ws, pid = ws_factory()
    _seed_two_chars(ws, pid, write_json)
    _write_gist_two_events(ws, pid)
    e1, e2, title = _event_reply_blocks()
    # 广播带思考抖动重试（broadcast_cast 默认 max_retries=2 → 每事件最多 3 次仍空 → 降级）
    bad = ["选角导演开始胡言乱语"] * 3
    replies = (bad + e1 + bad + e2 + [title])
    llm = _seq_llm(replies)
    res = _produce(ws, pid, llm, broadcast_casting=True)
    assert res.ok, res.result
    assert res.broadcasts_built == 0
    assert not ws._abs(f"{pid}/memory/castings").exists()
    draft = ws.draft_path(pid, 1, 1)
    assert draft.exists()
    text = draft.read_text(encoding="utf-8")
    assert "月下试探" in text and "入夜，两人在月下相对" in text  # 降级路径照常出稿（文本命中兜底 cast）
