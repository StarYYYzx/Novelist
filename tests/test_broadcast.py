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
        "入夜，两人在月下相对。叶岚试探着问起玉佩，苏晚别开脸，只说不知。",  # 正文
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
    replies = (["选角导演开始胡言乱语"] + e1      # e1 广播：解析失败 → None
               + ["选角导演开始胡言乱语"] + e2 + [title])
    llm = _seq_llm(replies)
    res = _produce(ws, pid, llm, broadcast_casting=True)
    assert res.ok, res.result
    assert res.broadcasts_built == 0
    assert not ws._abs(f"{pid}/memory/castings").exists()
    draft = ws.draft_path(pid, 1, 1)
    assert draft.exists()
    text = draft.read_text(encoding="utf-8")
    assert "月下试探" in text and "入夜，两人在月下相对" in text  # 降级路径照常出稿（文本命中兜底 cast）
