"""M19 里程碑测试：ADR-020 生成期人物一致性四件套 + 归因 P0 回归。

覆盖（docs/03 ADR-020）：
- 决策一 延迟拟题：细纲行内不写标题、事件 prompt 禁标题、章末拟题清洗与回填、
  标题去重 search 匹配行内变体；
- 决策二 无条件注入：match_cast 姓名/别名/子串回退、9 字段渲染；
- 决策三 人物调度层：调度单解析只留 cast、build_direction 成功/静默降级、落盘；
- 决策四 角色视角记忆：record_perspectives 事件末多视角写入、同事件去重、
  needs_readback 双阈值（事件计数 ≥3 或 天数 ≥30）、近况渲染；
- 归因 P0 回归：strip_seam_overlap 全片段扫描（scan_sents=40）、
  polish 透传 system_prompt、completeness 重复检测。

单元测试绝不真调 LLM（docs/09 §2.1）：LLM 走 stub_llm / StubLLM。
"""

from __future__ import annotations

from novelist.core import timeline as tl
from novelist.core import worldstate
from novelist.core.chronicler import Chronicler
from novelist.core.director import (
    DirectionSheet,
    _parse_directions,
    build_direction,
    cast_from_text,
    load_characters,
    match_cast,
    needs_readback,
    recent_perspectives,
    render_card_line,
    render_cards,
    render_history_lines,
    save_direction,
)
from novelist.core.memory import MemoryWriter
from novelist.core.orchestrator import (
    _apply_chapter_title,
    _dedupe_chapter_titles,
    _event_goal,
    _title_chapter,
    strip_seam_overlap,
)
from novelist.core.polish import completeness, polish_chapter
from novelist.forge.nodes import render_gist_md


def _seed_chars(ws, pid, write):
    write(ws, pid, "bible/characters.json", [
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


# ---------------------------------------------------------------- 决策一：延迟拟题


def test_render_gist_md_inline_has_no_title_text(ws_factory):
    """细纲行内 `# 第 N 章` 不带标题文字（防注入正文 prompt），title 只在 front-matter。"""
    gist = {"vol": 1, "ch": 3, "title": "觉醒", "pov": "第三人称限知（叶岚视角）",
            "key_events": ["系统觉醒", "绑定云曦"], "characters": ["char:yelan"],
            "after_days": 2}
    md = render_gist_md(gist, 1, 3, ["叶岚"])
    assert f"# 第 3 章 觉醒" not in md
    assert "# 第 3 章\n" in md
    assert '"title": "觉醒"' in md  # front-matter 保留（parse_gist / 人读）


def test_event_goal_bans_chapter_title():
    prompt = _event_goal("撰写本章", "叶岚下山", 1, 2, "", 900, [], False)
    assert "不要写章节标题" in prompt
    assert "不要写「第X章」字样" in prompt


def test_dedupe_chapter_titles_search_inline_variant():
    """标题带行内变体（缩进/后缀正文）也能删；正文引用「第X章」不算标题不删。"""
    text = ("# 第 1 章 脚步声\n\n正文一段。\n\n"
            "　## 第 1 章 脚步声　叶岚推开门\n\n正文二段。\n\n"
            "正文里回顾「第 1 章」的情节不算标题。")
    out = _dedupe_chapter_titles(text)
    assert out.count("# 第 1 章") == 1  # 只保留首个标题
    assert "正文一段" in out and "正文二段" in out  # 正文不丢
    assert "回顾「第 1 章」" in out  # 正文引用保留


def test_title_chapter_cleans_prefix(ws_factory, stub_llm):
    ws, pid = ws_factory()
    provider = stub_llm("## 第3章 觉醒之夜")
    t = _title_chapter(provider, "正文……叶岚握紧玉佩。", 1, 3)
    assert t == "觉醒之夜"  # # 号与「第X章」前缀剥掉


def test_apply_chapter_title_writes_back_and_fm(ws_factory, stub_llm):
    ws, pid = ws_factory()
    p = ws.outline_chapter_path(pid, 1, 3)
    ws.write_text(p, '---\n{"vol": 1, "ch": 3, "title": "旧题"}\n---\n\nkey_events: []\n')
    final = "正文成稿内容。"
    out, title = _apply_chapter_title(ws, pid, stub_llm("夜雨试剑"), final, 1, 3)
    assert title == "夜雨试剑"
    assert out.startswith("# 第 3 章 夜雨试剑")  # 写回正文首行
    fm = p.read_text(encoding="utf-8")
    assert '"title": "夜雨试剑"' in fm  # 回填 front-matter


# ---------------------------------------------------------------- 决策二：无条件注入


def test_match_cast_name_alias_substring():
    cards = [
        {"id": "c1", "name": "叶岚", "aliases": ["叶师兄"]},
        {"id": "c2", "name": "苏晚", "aliases": ["苏师姐"]},
        {"id": "c3", "name": "云清瑶"},
    ]
    assert [c["id"] for c in match_cast(cards, ["苏师姐", "叶岚"])] == ["c2", "c1"]
    # 称呼近似（"云清"是"云清瑶"子串）也能回退，不产生新卡
    assert [c["id"] for c in match_cast(cards, ["云清"])] == ["c3"]
    # 圣经外名字跳过（不造人）
    assert match_cast(cards, ["路人之名"]) == []


def test_cast_from_text_long_name_first_no_double_hit():
    cards = [
        {"id": "c1", "name": "云清瑶", "aliases": []},
        {"id": "c2", "name": "清瑶", "aliases": []},
    ]
    got = cast_from_text(cards, "云清瑶冷冷看了一眼。")
    assert [c["id"] for c in got] == ["c1"]  # 长名先命中，短名不再重复


def test_render_card_line_ten_fields():
    line = render_card_line({
        "id": "c1", "name": "叶岚", "gender": "male",
        "power": {"level": "炼气三层", "faction": "青云宗"},
        "core_traits": ["冷静", "心机深"],
        "arc": "弃徒到中坚", "relationships": [{"target": "char:sw", "type": "青梅竹马"}],
        "behavior_rules": ["遇险先示弱", "不主动暴露系统"],
        "aliases": ["叶师兄"], "possessions": ["残玉"], "status": "active"})
    assert "弧线" in line and "关系" in line and "称谓" in line and "持有" in line
    assert "行为：遇险先示弱；不主动暴露系统" in line  # M3r 补喂字段进注入面


def test_render_card_line_without_rules_keeps_old_shape():
    # 无 behavior_rules 的旧卡：不出现"行为"段（向后兼容，行结构与改动前一致）
    line = render_card_line({
        "id": "c1", "name": "叶岚", "gender": "male",
        "power": {"level": "炼气"}, "relationships": [{"target": "char:sw", "type": "青梅竹马"}]})
    assert "行为" not in line
    assert line.startswith("叶岚｜男｜炼气") and "关系" in line


def test_render_cards_unconditional_lines():
    cards = [{"id": "c1", "name": "叶岚", "gender": "male", "power": {"level": "炼气"}}]
    assert render_cards(cards) == ["- 叶岚｜男｜炼气"]


def test_render_card_line_skips_unknown_gender():
    # gender=unknown 是 forge/模板占位，不是信息——不进 prompt
    line = render_card_line({"id": "c1", "name": "五五开系统", "gender": "unknown"})
    assert line == "五五开系统"
    assert "unknown" not in line


def test_render_cards_resolves_rel_target_to_name():
    # 关系目标 char:xxx 应显示角色名而非半英文 id（模型认"云清瑶"不认"yun"）
    chars = [
        {"id": "char:yelan", "name": "叶岚", "gender": "male",
         "relationships": [{"target": "char:yun", "type": "绑定对象"}]},
        {"id": "char:yun", "name": "云清瑶", "gender": "female"},
    ]
    lines = render_cards(chars)
    assert any("关系：云清瑶（绑定对象）" in ln and "yun" not in ln for ln in lines)
    # 目标不在卡集里时回退 id 尾段（不崩）
    solo = render_card_line({"id": "char:yelan", "name": "叶岚",
                             "relationships": [{"target": "char:sw", "type": "青梅竹马"}]})
    assert "关系：sw（青梅竹马）" in solo
    # 非 char: 前缀（组织/物品）原样
    org = render_card_line({"id": "char:yelan", "name": "叶岚",
                            "relationships": [{"target": "青云宗", "type": "弟子"}]})
    assert "关系：青云宗（弟子）" in org


# ---------------------------------------------------------------- 决策三：人物调度层


def test_parse_directions_keeps_only_cast_names():
    content = ("- 叶岚 | 冷静、心机深 | 话少，先听再答 | 不得自称小姐\n"
               "- 苏晚 | 刚烈 | 抢白 | 不得卑躬屈膝\n"
               "- 虚构角色 | 油滑 | 抢戏 | 无")  # 圣经外名字应被过滤
    out = _parse_directions(content, ["叶岚", "苏晚"])
    assert [d.name for d in out] == ["叶岚", "苏晚"]
    assert out[0].taboo and out[0].how


def test_build_direction_success_and_sheet_lines(ws_factory, stub_llm, write_json):
    ws, pid = ws_factory()
    _seed_chars(ws, pid, write_json)
    reply = ("叶岚 | 冷静、心机深 | 话少，先听再答 | 不得自称小姐\n"
             "苏晚 | 刚烈、护短 | 抢白护着叶岚 | 不得示弱\n")
    sheet = build_direction(ws, pid, stub_llm(reply), vol=1, ch=1, event_index=2,
                            ev_text="叶岚下山", cards=load_characters(ws, pid))
    assert isinstance(sheet, DirectionSheet) and len(sheet.characters) == 2
    lines = sheet.lines()
    assert lines and lines[0].startswith("- 叶岚")


def test_build_direction_degrades_silently(ws_factory, stub_llm):
    """LLM 阻塞 / 空输出 / 全噪音 → None（调用方降级为仅注入人物卡）。"""
    ws, pid = ws_factory()
    cards = load_characters(ws, pid)
    assert build_direction(ws, pid, stub_llm(""), vol=1, ch=1, event_index=1,
                           ev_text="x", cards=cards) is None
    assert build_direction(ws, pid, stub_llm("无法解析"), vol=1, ch=1, event_index=1,
                           ev_text="x", cards=cards) is None
    assert build_direction(ws, pid, None, vol=1, ch=1, event_index=1,
                           ev_text="x", cards=cards) is None


def test_save_direction_persists(ws_factory):
    ws, pid = ws_factory()
    sheet = DirectionSheet(vol=1, ch=3, event_index=2, event_text="下山")
    save_direction(ws, pid, sheet)
    p = ws.memory_dir(pid) / "directions" / "v1-c3-e2.json"
    assert p.exists()


# ---------------------------------------------------------------- 决策四：角色视角记忆


def test_record_perspectives_event_end_multi_view(ws_factory, stub_llm, write_json):
    """一次调用产出全部角色的视角条目（事件末调用，多视角差异）。"""
    ws, pid = ws_factory()
    _seed_chars(ws, pid, write_json)
    reply = ("叶岚 | 受挫、记恨 | 被威压压制，记下了这笔账 | 苏晚：记恨加深\n"
             "苏晚 | 担忧 | 想替叶岚出头却忍住了 | 叶岚：心疼\n")
    c = Chronicler(ws, pid, llm=stub_llm(reply))
    written = c.record_perspectives("正文（叶岚被云清瑶威压压制）……", ["叶岚", "苏晚"],
                                    1, 2, event_index=1)
    assert written == 2
    for cid in ("char:yelan", "char:sw"):
        ents = recent_perspectives(ws, pid, cid, limit=1)
        assert ents, f"{cid} 应有视角条目"
        e = ents[0]
        assert e["kind"] == "perspective"
        assert e["summary"].startswith("[视角]")
        assert e["at"]["ch"] == 2 and e["event_ref"].endswith(":e1")
    yl = recent_perspectives(ws, pid, "char:yelan", limit=1)[0]
    assert yl["stance"] == "受挫、记恨" and yl["relations"]  # 关系增量在


def test_record_perspectives_same_event_dedup(ws_factory, stub_llm, write_json):
    """同事件同人重复调用：MemoryConflictError 被吞，written 不再累计。"""
    ws, pid = ws_factory()
    _seed_chars(ws, pid, write_json)
    reply = "叶岚 | 受挫 | 记下了这笔账\n"
    c = Chronicler(ws, pid, llm=stub_llm(reply))
    assert c.record_perspectives("正文……", ["叶岚"], 1, 2, event_index=1) == 1
    assert c.record_perspectives("正文……", ["叶岚"], 1, 2, event_index=1) == 0


def test_record_perspectives_writes_into_character_histories_file(
        ws_factory, stub_llm, write_json):
    """落盘位置是 character_histories（实然层），不是 bible/characters.json。"""
    ws, pid = ws_factory()
    _seed_chars(ws, pid, write_json)
    c = Chronicler(ws, pid, llm=stub_llm("叶岚 | 冷静 | 什么都没说\n"))
    c.record_perspectives("正文……", ["叶岚"], 1, 2, event_index=3)
    hist = ws.char_history_path(pid, "char:yelan")
    assert hist.exists()
    import json

    data = json.loads(hist.read_text(encoding="utf-8"))
    assert data["char_id"] == "char:yelan" and data["entries"][-1]["kind"] == "perspective"


# ---- 回读双阈值 ----

def test_needs_readback_event_gap_triggers(ws_factory, write_json):
    """距上次出场 ≥3 个事件 → 回读（主判据：事件计数）。"""
    ws, pid = ws_factory()
    _seed_chars(ws, pid, write_json)
    evs = [{"id": f"ev{i}", "participants": ["char:yelan"] if i < 4 else []}
           for i in range(8)]  # 叶岚最后出场在倒数第 5 条（i=3）→ 倒序 gap=4
    write_json(ws, pid, "memory/plot_events.json", evs)
    need, why = needs_readback(ws, pid, "char:yelan")
    assert need and "事件" in why


def test_needs_readback_day_gap_triggers(ws_factory, write_json):
    """事件 gap 小但故事内 ≥30 天未见 → 回读（辅判据：时间线天数）。"""
    ws, pid = ws_factory()
    _seed_chars(ws, pid, write_json)
    evs = [{"id": "ev0", "participants": ["char:yelan"]},
           {"id": "ev1", "participants": ["char:yelan"]}]  # 事件 gap=0
    write_json(ws, pid, "memory/plot_events.json", evs)
    # 造视角记录 t=0，推进时间到 40 天
    writer = MemoryWriter(ws, pid)
    writer.append_experience("char:yelan", {
        "summary": "[视角] 初见", "at": {"vol": 1, "ch": 1, "t": 0}, "kind": "perspective"})
    tl.advance(ws, pid, 40, vol=1, ch=2, event="闭关")
    need, why = needs_readback(ws, pid, "char:yelan")
    assert need and "天未出场" in why


def test_needs_readback_recent_not_trigger(ws_factory, write_json):
    ws, pid = ws_factory()
    _seed_chars(ws, pid, write_json)
    evs = [{"id": "ev0", "participants": ["char:yelan"]},
           {"id": "ev1", "participants": ["char:yelan"]}]  # 最后一条就是本角色
    write_json(ws, pid, "memory/plot_events.json", evs)
    need, _why = needs_readback(ws, pid, "char:yelan")
    assert not need


def test_render_history_lines_unconditional(ws_factory, stub_llm, write_json):
    """出场即注入近况行（无条件，不做检索筛选），带 stance。"""
    ws, pid = ws_factory()
    _seed_chars(ws, pid, write_json)
    Chronicler(ws, pid, llm=stub_llm("叶岚 | 受挫、记恨 | 记下了这笔账\n")).record_perspectives(
        "正文……", ["叶岚"], 1, 2, event_index=1)
    cards = [c for c in load_characters(ws, pid) if c["id"] == "char:yelan"]
    lines = render_history_lines(ws, pid, cards, limit=3)
    assert lines and "叶岚" in lines[0] and "受挫" in lines[0] and "记下" in lines[0]


# ---------------------------------------------------------------- P0 回归（前两章归因）


def test_strip_seam_overlap_scans_whole_piece():
    """复述出现在第 13 句之后也必须删（scan_sents 12→40 的回归用例）。"""
    prev = "……" + ("前文内容甲。" * 20) + " 叶岚缓缓推开了那扇沉重的石门。"
    piece = "".join(f"新内容第{i}句。" for i in range(16)) + "叶岚缓缓推开了那扇沉重的石门。"
    out = strip_seam_overlap(prev, piece)
    assert "石门" not in out  # 尾句复述被删（正文够长，不触发 <30 字兜底）
    assert "新内容第0句" in out  # 正文保留


def test_strip_seam_overlap_low_sim_high_zone():
    """开头 3 句复述高发区用 sim_first=0.58：近似复述（改了个别字）也能删。"""
    prev = "……" + "他握紧了那枚残玉，目光沉了下去。"
    piece = ("他握紧那枚残玉，目光沉了下来。\n"  # 近似复述（换字）
             "然后他转身走出洞府，踩着露水往山下走去，背影很快隐入晨雾，"
             "只留下一串深浅不一的脚印。")
    out = strip_seam_overlap(prev, piece)
    assert "握紧那枚残玉" not in out
    assert "转身走出洞府" in out


def test_polish_chapter_forwards_system_prompt(ws_factory):
    """润色调用透传 system_prompt（此前无 system 是归因 P0）。"""
    from novelist.core.llm import LLMMessage, LLMResult

    captured: list[str] = []

    class _Cap:
        def complete(self, req):
            captured.extend(m.content for m in req.messages if m.role == "system")
            return LLMResult(ok=True, content="润色后的正文。", finish_reason="stop",
                             blocked=False)

    out = polish_chapter("这是正文内容，需要润色。", _Cap(),
                         system_prompt="你是修仙文作者，文风冷肃。")
    assert out.changed and "润色后" in out.text
    assert captured and captured[0] == "你是修仙文作者，文风冷肃。"


def test_completeness_detects_duplicate_paragraphs():
    """重复检测（此前 completeness 0 告警的回归用例）：整段重复要报出来。"""
    body = ("第一章正文。他走进洞府，看见桌上放着一枚残玉。\n" * 3 +
            "结尾收束，他合上了眼。")
    comp = completeness(body)
    assert comp["dup_paragraphs"] >= 2
    assert comp["dup_sentences"] >= 2


# ---------------------------------------------------------------- 回归：ADR-020 计数器回传

def _seq_llm(replies):
    """队列式 LLM 替身：逐次弹出固定回复，并记录每次调用的最后一条 user 内容。"""
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


def test_event_loop_adr020_counters_reach_result(ws_factory, write_json):
    """端到端回归（2026-09-02 真机发现）：produce_chapter 出口的 ProductionResult
    构造漏传 chapter_title / directions_built / perspectives_written → 恒为 0。
    必须整链路断言计数器回传，不能只测单函数。"""
    from novelist.core.orchestrator import produce_chapter
    from novelist.core.session import SessionInfo

    ws, pid = ws_factory()
    _seed_chars(ws, pid, write_json)
    gist = ws.outline_chapter_path(pid, 1, 1)
    gist.parent.mkdir(parents=True, exist_ok=True)
    # key_events 文本含圣经人物名（叶岚/苏晚）→ cast_from_text 兜底命中
    gist.write_text(
        "---\nvol: 1\nch: 1\ntitle: 弃徒\n"
        "key_events: [叶岚下山拾玉, 叶岚与苏晚月下夜谈]\n---\n\n正文要点",
        encoding="utf-8")

    # 每事件 5 次：调度(direction) → 正文(gen) → 审校(ok) → 编纂(chronicler) → 视角(perspectives)
    # 2 事件 = 10 次 + 章末拟题 1 次
    llm = _seq_llm([
        # ---- event 1 ----
        "叶岚 | 冷静、心机深 | 话少先观察 | 不得自曝穿越\n"
        "苏晚 | 刚烈、护短 | 暗中跟随 | 不得示弱",                       # 调度单
        "叶岚下了山，在山道旁拾起半枚焦黑玉佩，掌心发烫，他垂眼收进怀里。",  # 正文
        "ok",                                                            # 审校（无 block）
        "拾玉 | discovery | 叶岚",                                      # 编纂
        "叶岚 | 警觉 | 这玉佩来得蹊跷 | 苏晚：无\n"                       # 视角
        "苏晚 | 好奇 | 想追问却忍住了 | 叶岚：更神秘了",
        # ---- event 2 ----
        "叶岚 | 冷静、心机深 | 试探苏晚来历 | 不得说出前世记忆\n"
        "苏晚 | 刚烈、护短 | 咬唇不语 | 不得泄露宗门密辛",                # 调度单
        "入夜，两人在月下相对。叶岚试探着问起玉佩，苏晚别开脸，只说不知。",  # 正文
        "ok",                                                            # 审校
        "夜谈 | plot | 叶岚、苏晚",                                     # 编纂
        "叶岚 | 审慎 | 苏晚有所隐瞒 | 苏晚：防备\n"                       # 视角
        "苏晚 | 挣扎 | 玉佩涉及宗门旧案，不能说 | 叶岚：愧疚",
        # ---- 章末拟题 ----
        "月下试探",                                                      # _title_chapter
    ])
    res = produce_chapter(
        ws, pid, 1, 1, llm, prefer_direct=True,
        inject_bible=False, event_loop=True, commit_chapter_event=False,
        knowledge_llm=False, event_polish=False, supplement_settings=False,
        session=SessionInfo(project_id=pid, agent="t"),
        # ADR-020 四件套全开（default 即开，显式写出强调本测试意图）
        defer_title=True, cast_injection=True,
        character_direction=True, perspective_memory=True)
    assert res.ok, res.result
    assert res.directions_built == 2, f"调度单计数必须回传，实际 {res.directions_built}"
    assert res.perspectives_written >= 2, \
        f"视角条数必须回传，实际 {res.perspectives_written}"
    assert res.chapter_title == "月下试探", f"拟题必须回传，实际 {res.chapter_title!r}"
    # 标题应写回草稿首行（_apply_chapter_title 副作用不受出口漏传影响）
    draft = ws.draft_path(pid, 1, 1)
    assert draft.exists() and "月下试探" in draft.read_text(encoding="utf-8")
