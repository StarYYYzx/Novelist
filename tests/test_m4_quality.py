"""M4 质量增强测试（B-02/03/04/07/08/09/13 + 文风优化）。

覆盖端到端系统测试暴露的缺口修复：

- **B-02 圣经注入**：`core/context.py` 按章节装配世界观/文风/出场人物卡/主角约束/输出纪律。
- **B-03 编纂员**：`core/chronicler.py` LLM 抽取 + 冲突双检 + 写入。
- **B-04 生成完整性**：`core/polish.py::completeness` 截断与元叙事检测。
- **B-07 记忆回退**：`MemoryWriter.drop_by_source` / `revise_fragment`。
- **B-08 审校**：确定性 R-LEX / R-PWR + LLM 语义审校 `Reviewer`。
- **B-09 敏感词**：默认词表非空 + 文件加载。
- **B-13 导出书名**：用 project.title 而非 project_id。
- **文风优化**：AI 味度量的确定性与润色的保底行为。
"""

from __future__ import annotations

import json

import pytest

from novelist.consistency import run_consistency
from novelist.consistency.reviewer import ReviewIssue, Reviewer
from novelist.consistency.rules import run_lexicon_checks, run_rule_checks
from novelist.core.chronicler import Chronicler, ExtractedEvent
from novelist.core.context import build_chapter_context, chapter_cast, load_bible
from novelist.core.export import export_project
from novelist.core.llm import LLMResult
from novelist.core.memory import MemoryIndex, MemoryWriter, rollback_chapter
from novelist.core.moderation import ModerationPrechecker, load_banned_words
from novelist.core.polish import completeness, measure, polish_chapter
from novelist.storage.checkpoint import Checkpoint
from novelist.storage.workspace import Workspace


def _project(tmp_path, pid="proj-q", title="测试书名"):
    ws = Workspace(root=str(tmp_path))
    ws.create_project(pid)
    Checkpoint(ws).save(pid, {"id": pid, "title": title, "pipeline_state": "正文", "event_seq": 0})
    return ws, pid


def _write(ws, pid, rel, data):
    ws.write_json(ws._abs(f"{pid}/{rel}"), data)


def _seed(ws, pid, *, with_style=True, with_world=True):
    _write(ws, pid, "bible/characters.json", [
        {"id": "char:sw", "name": "苏晚", "gender": "male", "status": "active",
         "core_traits": ["隐忍"], "power": {"level": "炼气三层", "faction": "青云宗"},
         "first_appear": {"vol": 1, "ch": 1}, "is_protagonist": True},
        {"id": "char:twy", "name": "铁无涯", "gender": "male", "status": "active",
         "power": {"level": "金丹初期", "faction": "青云宗"}, "first_appear": {"vol": 1, "ch": 1}},
        {"id": "char:zh", "name": "赵虎", "gender": "male", "status": "active",
         "power": {"level": "炼气五层", "faction": "青云宗"}, "first_appear": {"vol": 1, "ch": 2}},
    ])
    if with_style:
        _write(ws, pid, "bible/style.json", {
            "protagonist": {"id": "char:sw", "name": "苏晚", "gender": "male"},
            "pov": "第三人称限知", "tone": ["冷峻"], "narration": "短句为主",
            "forbidden_words": ["系统", "签到"], "target_words_per_chapter": 800,
        })
    if with_world:
        _write(ws, pid, "bible/worldview.json", {
            "name": "青冥界",
            "power_system": {"levels": ["炼气", "筑基", "金丹", "元婴"]},
            "rules": ["修士不可对凡人出手"],
        })


class _StubLLM:
    """返回固定文本的 LLM 替身（不调真实模型，docs/09 §2.1）。"""

    def __init__(self, reply: str = "", blocked: bool = False) -> None:
        self.reply = reply
        self.blocked = blocked
        self.calls = 0

    def complete(self, req):
        self.calls += 1
        if self.blocked:
            return LLMResult(ok=False, blocked=True, block_reason="test", provider="stub")
        return LLMResult(ok=True, content=self.reply, provider="stub")


# ---------------------------------------------------------------- B-02 圣经注入


def test_context_injects_worldview_style_and_cast(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    gist = ws.outline_chapter_path(pid, 1, 2)
    gist.parent.mkdir(parents=True, exist_ok=True)
    gist.write_text("细纲：赵虎抢玉佩，苏晚反击。", encoding="utf-8")

    ctx = build_chapter_context(ws, pid, 1, 2)
    sp = ctx.system_prompt
    assert "青冥界" in sp, "世界观必须进上下文"
    assert "炼气、筑基、金丹、元婴" in sp, "境界体系必须进上下文"
    assert "修士不可对凡人出手" in sp, "世界铁律必须进上下文"
    assert "第三人称限知" in sp and "冷峻" in sp, "文风必须进上下文"
    assert "苏晚" in sp and "铁无涯" in sp, "人物名单（防造人）必须进上下文"
    assert "赵虎" in sp, "细纲点名的人物应出场"
    assert "系统" in sp and "签到" in sp, "禁用词必须进上下文"
    assert "苏晚" in ctx.user_goal and "赵虎抢玉佩" in ctx.user_goal, "细纲必须进 user goal"


def test_context_enforces_protagonist_pronoun(tmp_path):
    """主角性别漂移的直接防线（首次直出把男主写成"她"）。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    sp = build_chapter_context(ws, pid, 1, 1).system_prompt
    assert "主角是苏晚" in sp
    assert "一律用「他」" in sp
    assert "绝不可混用" in sp


def test_context_excludes_characters_not_yet_appeared(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    cast = chapter_cast(load_bible(ws, pid)["characters"], 1, 1, "细纲：苏晚被逐。")
    ids = [c["id"] for c in cast]
    assert "char:sw" in ids, "主角恒在场"
    assert "char:zh" not in ids, "first_appear 在 1:2 的赵虎不应出现在第 1 章"


def test_context_includes_output_discipline(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    sp = build_chapter_context(ws, pid, 1, 1).system_prompt
    assert "元叙事" in sp, "必须禁止正文出现「第X章」"
    assert "完整性" in sp, "必须要求结尾完整收束"


def test_context_goal_strips_gist_title_line(tmp_path):
    """ADR-020 决策一真机回归：细纲首行「# 第 X 章 <标题>」不得进生成 goal。

    qwen3.6 实测：标题留在上下文，模型会概率性在事件边界复述成
    「## 第X章 <细纲标题>」卡进正文（ch2/ch5 元叙事泄漏）。forge 的
    render_gist_md 剥过，但 produce_chapter 走 context 读 outline 原文，
    此路径必须同样剥离。key_events/要点要保留。
    """
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    gist = "# 第 1 章 觉醒之夜\n\nkey_events: [苏晚踏入山门遇袭]\n\n细纲要点：\n- 苏晚登场。"
    ctx = build_chapter_context(ws, pid, 1, 1, gist_text=gist)
    assert "第 1 章 觉醒之夜" not in ctx.user_goal, "细纲标题泄漏进生成 goal"
    assert "觉醒之夜" not in ctx.user_goal
    assert "key_events" in ctx.user_goal and "苏晚踏入山门遇袭" in ctx.user_goal, "事件清单必须保留"
    assert "苏晚登场" in ctx.user_goal, "细纲要点必须保留"
    # 无标题行的细纲不受影响
    plain = "key_events: [苏晚踏入山门遇袭]\n\n细纲要点：\n- 苏晚登场。"
    ctx2 = build_chapter_context(ws, pid, 1, 1, gist_text=plain)
    assert "key_events" in ctx2.user_goal


def test_context_injects_worldview_extra_fields(tmp_path):
    """装配补读：power_system.note / summary / civilizations / systems / realm_fluctuates
    不能因 build_system_prompt 没读就静默丢失（proj-yelan 实测：境界波动角色设定 0 条进上下文）。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    _write(ws, pid, "bible/worldview.json", {
        "name": "青冥界",
        "summary": "五境修仙界，宗门林立，灵脉之争暗流涌动",
        "power_system": {
            "levels": ["炼气", "筑基", "金丹", "元婴"],
            "note": "炼气纳灵气、筑基筑道基、金丹凝丹、元婴出窍",
        },
        "rules": ["修士不可对凡人出手"],
        "civilizations": ["人族", "妖族"],
        "systems": ["宗门体系"],
        "realm_fluctuates": ["char:sw", "苏晚"],
    })
    sp = build_chapter_context(ws, pid, 1, 1).system_prompt
    assert "青冥界" in sp
    assert "炼气纳灵气" in sp, "power_system.note 必须进上下文（此前 6/6 项目全丢）"
    assert "五境修仙界" in sp, "worldview.summary 必须进上下文"
    assert "人族、妖族" in sp, "civilizations 必须进上下文"
    assert "宗门体系" in sp, "systems 必须进上下文"
    assert "境界波动" in sp and "苏晚" in sp, "realm_fluctuates 必须作为纪律行进上下文"


def test_context_injects_worldview_modern_words(tmp_path):
    """装配补读：worldview.modern_words（现代词禁令）须进上下文——与端到端教训
    「修仙文出现『厚眼镜』」直接相关（forge 写了这份清单，装配层此前不读）。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    _write(ws, pid, "bible/worldview.json", {
        "name": "青冥界",
        "power_system": {"levels": ["炼气", "筑基", "金丹", "元婴"]},
        "rules": ["修士不可对凡人出手"],
        "modern_words": ["厚眼镜", "WiFi", "白领"],
    })
    sp = build_chapter_context(ws, pid, 1, 1).system_prompt
    assert "禁用现代词" in sp
    assert "厚眼镜" in sp and "WiFi" in sp and "白领" in sp


# ---------------------------------------------------------------- B-03 编纂员


def test_chronicler_parses_events_and_resolves_names(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    llm = _StubLLM("苏晚被逐出内门 | conflict | 苏晚,铁无涯\n苏晚拾得断玉佩 | discovery | 苏晚\n")
    c = Chronicler(ws, pid, llm=llm)
    ex = c.extract("正文略")
    events = ex.events
    assert len(events) == 2
    assert events[0].kind == "conflict"
    assert events[0].participant_ids == ["char:sw", "char:twy"]
    assert events[1].kind == "discovery"
    assert events[1].participant_ids == ["char:sw"]


def test_chronicler_backfills_names_omitted_by_model(tmp_path):
    """模型常漏给人物名，须能从简述里回扫圣经姓名。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    c = Chronicler(ws, pid, llm=_StubLLM("铁无涯宣布末位者逐出内门 | conflict\n"))
    assert c.extract("正文略").events[0].participant_ids == ["char:twy"]


def test_chronicler_commits_with_conflict_check(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    c = Chronicler(ws, pid, llm=_StubLLM("苏晚拾得断玉佩 | discovery | 苏晚\n"))
    report = c.run("正文略", 1, 1)
    assert report.written == 1 and not report.conflicts
    events = json.loads(ws._abs(f"{pid}/memory/plot_events.json").read_text(encoding="utf-8"))
    assert events[-1]["type"] == "discovery"
    # 人物经历也写入
    hist = json.loads(ws.char_history_path(pid, "char:sw").read_text(encoding="utf-8"))
    assert hist["entries"][-1]["summary"] == "苏晚拾得断玉佩"


def test_chronicler_rejects_duplicate_on_second_run(tmp_path):
    """重复入库即冲突，回退不静默（docs/06 §4.4）。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    c = Chronicler(ws, pid, llm=_StubLLM("苏晚拾得断玉佩 | discovery | 苏晚\n"))
    assert c.run("正文略", 1, 1).written == 1
    second = c.run("正文略", 1, 1)
    # P0-C 后重复由近似去重闸门先行拦截（rejected），完全同 sig 走 writer 冲突——
    # 两条路径都保证"不静默入库"
    assert second.written == 0 and (second.conflicts or second.rejected), \
        "第二次应判重复并回退"


def test_chronicler_without_llm_returns_nothing(tmp_path):
    """无 LLM 时编纂不可用，不做假。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    ex = Chronicler(ws, pid, llm=None).extract("正文")
    assert ex.events == [] and ex.state_changes == [] and ex.time_lines == []


def test_chronicler_semantic_layer_can_block(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    c = Chronicler(ws, pid, llm=_StubLLM("苏晚战死 | conflict | 苏晚\n"),
                   semantic_checker=lambda new, existing: False)
    report = c.run("正文略", 1, 1)
    assert report.written == 0 and any("语义冲突" in x for x in report.conflicts)
    assert not ws._abs(f"{pid}/memory/plot_events.json").exists(), "冲突不得入库"


# ---------------------------------------------------------------- B-04 完整性


def test_completeness_detects_truncation():
    assert completeness("他站在山门前，望着远方").get("ends_properly") is False
    assert completeness("他站在山门前，望着远方。").get("ends_properly") is True


def test_completeness_ignores_title_line_meta_narration():
    """标题行本来就该写「第X章」，不能算泄漏。"""
    text = "## 第 1 章 弃徒\n\n他走下山阶，拾起半枚玉佩。"
    assert completeness(text)["meta_narration"] == []


def test_completeness_flags_body_meta_narration():
    text = "## 第 1 章 弃徒\n\n那是他在第一章捡到的玉佩。"
    assert "第一章" in " ".join(completeness(text)["meta_narration"])


# ---------------------------------------------------------------- 文风度量与润色


def test_ai_tone_metric_is_deterministic():
    a = measure("仿佛有什么东西碎了。不是害怕，而是愤怒。这一刻，他知道。")
    b = measure("仿佛有什么东西碎了。不是害怕，而是愤怒。这一刻，他知道。")
    assert a.score == b.score and a.signals == b.signals


def test_ai_tone_metric_penalises_cliches():
    cliche = ("仿佛有什么东西碎了。不是害怕，而是愤怒。这一刻，他知道了真相。"
              "剑气锐利如刀，眼神深邃如渊——他明白，从这一刻起，他不再是弃徒。")
    plain = "玉佩碎了。他弯腰捡起来，揣进怀里，继续下山。"
    assert measure(cliche).score > measure(plain).score


def test_polish_keeps_original_when_no_improvement(tmp_path):
    """润色若没变好（或把章节弄坏），保留原文。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    original = "他走下山阶，拾起半枚玉佩，揣进怀里。风很大。\n"
    bad = _StubLLM("。")  # 明显更差且过短
    res = polish_chapter(original, bad)
    assert res.changed is False and res.text == original


def test_polish_applies_when_better(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    cliche = (
        "## 第一章 弃徒\n\n"
        "仿佛有什么东西碎了。不是害怕，而是愤怒。这一刻，他知道自己的处境有多危险。\n\n"
        "剑气锐利如刀，眼神深邃如渊——他明白，从这一刻起，他不再是那个任人宰割的弃徒。\n\n"
        "他仿佛看见远方的山门，似乎又听见了谁的叹息。他知道，这一切都注定了。\n"
    )
    improved = (
        "## 第一章 弃徒\n\n"
        "玉碎了。他弯腰把碎片拢进袖子。\n\n"
        "山风从背后来，很硬。他继续往下走，没有回头。\n\n"
        "石阶湿，脚下一滑，他扶住崖壁站定。远处传来钟声，三响。他数着，等钟停了才迈步。\n"
    )
    res = polish_chapter(cliche, _StubLLM(improved))
    assert res.changed is True, res.note
    assert res.delta < 0, f"AI 味应下降，实际 {res.delta}"


def test_polish_keeps_original_when_output_truncated(tmp_path):
    """润色结果若被截断（没有完整收尾），保留原稿。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    original = "## 第一章 弃徒\n\n他走下山阶，拾起半枚玉佩。风很大。\n"
    res = polish_chapter(original, _StubLLM("## 第一章 弃徒\n\n玉碎了。他弯腰"))
    assert res.changed is False and res.text == original


def test_polish_skips_when_provider_missing():
    text = "他走下山阶。"
    res = polish_chapter(text, None)
    assert res.changed is False and "no llm" in res.note


# ---------------------------------------------------------------- B-08 审校


def test_rule_lexicon_catches_modern_word(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    d = ws._abs(f"{pid}/chapters")
    d.mkdir(parents=True, exist_ok=True)
    (d / "1-1.md").write_text("他推了推鼻梁上的厚眼镜，神色严谨。", encoding="utf-8")
    alerts = run_lexicon_checks(ws, pid)
    assert any(a.rule_id == "R-LEX" and "眼镜" in a.detail for a in alerts)


def test_rule_lexicon_catches_western_allusion(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    d = ws._abs(f"{pid}/chapters")
    d.mkdir(parents=True, exist_ok=True)
    (d / "1-1.md").write_text("旧约如达摩克利斯之剑悬在头顶。", encoding="utf-8")
    assert any("达摩克利斯" in a.detail for a in run_lexicon_checks(ws, pid))


def test_rule_lexicon_simile_not_disabled_by_empty_modern_words(tmp_path):
    """v7 真机回归（ch4「指甲刮过黑板」）：穿越文 worldview.modern_words=[] 会
    覆盖掉默认现代词表（叶岚前世的手机/电脑合理），但「黑板」这类叙事喻体
    （修辞性现代物）必须仍被 R-LEX 拦截——喻体表独立常开。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    _write(ws, pid, "bible/worldview.json", {"modern_words": []})
    d = ws._abs(f"{pid}/chapters")
    d.mkdir(parents=True, exist_ok=True)
    (d / "1-1.md").write_text("面具下传出阴冷的笑声，像指甲刮过黑板。", encoding="utf-8")
    hits = [a for a in run_lexicon_checks(ws, pid) if "黑板" in a.detail]
    assert hits and hits[0].level == "block", "喻体词不被 modern_words=[] 豁免"


def test_rule_lexicon_simile_respects_exempt(tmp_path):
    """喻体词豁免走 modern_words_exempt（与指称型同表）。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    _write(ws, pid, "bible/worldview.json",
           {"modern_words": [], "modern_words_exempt": ["黑板"]})
    d = ws._abs(f"{pid}/chapters")
    d.mkdir(parents=True, exist_ok=True)
    (d / "1-1.md").write_text("笑声像指甲刮过黑板。", encoding="utf-8")
    assert not any("黑板" in a.detail for a in run_lexicon_checks(ws, pid))


def test_rule_lexicon_catches_style_banned_word(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    d = ws._abs(f"{pid}/chapters")
    d.mkdir(parents=True, exist_ok=True)
    (d / "1-1.md").write_text("系统提示：叮，签到成功。", encoding="utf-8")
    hits = [a for a in run_lexicon_checks(ws, pid) if "禁用词" in a.detail]
    assert len(hits) == 2  # 系统 / 签到


def test_rule_power_system_detects_mixed_levels(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    d = ws._abs(f"{pid}/chapters")
    d.mkdir(parents=True, exist_ok=True)
    (d / "1-1.md").write_text("他是炼气三层，对面却是炼气一重的好手。", encoding="utf-8")
    alerts = [a for a in run_lexicon_checks(ws, pid) if a.rule_id == "R-PWR"]
    assert alerts and "炼气" in alerts[0].object_ref


def test_rule_power_system_ok_when_consistent(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    d = ws._abs(f"{pid}/chapters")
    d.mkdir(parents=True, exist_ok=True)
    (d / "1-1.md").write_text("他是炼气三层，对面是筑基初期。", encoding="utf-8")
    assert not [a for a in run_lexicon_checks(ws, pid) if a.rule_id == "R-PWR"]


def test_reviewer_parses_issues(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    llm = _StubLLM("block | 称谓失当 | 筑基修士称炼气弟子为前辈 | 改为直呼其名\nwarn | 人设漂移 | 语气偏软 | 更冷硬\n")
    issues = Reviewer(ws, pid, llm).review("正文", 1, 1)
    assert len(issues) == 2
    assert issues[0].level == "block" and issues[0].category == "称谓失当"
    assert issues[1].level == "warn"


def test_reviewer_returns_empty_when_ok(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    assert Reviewer(ws, pid, _StubLLM("ok")).review("正文", 1, 1) == []


def test_reviewer_scope_hint_enters_prompt(tmp_path):
    """qwen3.6 真机回归：事件级审校必须带「片段范围」说明，否则模型把单事件当
    整章、拿整章细纲对照报「细纲未覆盖」误 block → 修订污染事件边界。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    seen = {}

    class CaptureLLM(_StubLLM):
        def complete(self, req):
            seen["prompt"] = req.messages[-1].content
            return super().complete(req)

    llm = CaptureLLM("ok")
    scope = ("【范围说明】本次审读的是本章第 2/3 个事件的正文片段（本章尚未写完）。"
             "本章后续事件的内容尚未出现，不构成「细纲未覆盖」。")
    Reviewer(ws, pid, llm).review("正文", 1, 1, scope=scope)
    assert "第 2/3 个事件" in seen["prompt"]
    assert "后续事件的内容尚未出现" in seen["prompt"], "片段语义必须写进 prompt"


def test_run_consistency_without_llm_is_rule_layer_only(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    alerts = run_consistency(ws, pid)
    assert all(a.rule_id != "R-SEM" for a in alerts)


def test_run_consistency_with_llm_appends_semantic(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    d = ws._abs(f"{pid}/chapters")
    d.mkdir(parents=True, exist_ok=True)
    (d / "1-1.md").write_text("他走下山阶。", encoding="utf-8")
    llm = _StubLLM("block | 设定矛盾 | 违反铁律 | 改写\n")
    alerts = run_consistency(ws, pid, llm=llm)
    sem = [a for a in alerts if a.rule_id == "R-SEM"]
    assert sem and sem[0].level == "block" and "设定矛盾" in sem[0].object_ref


# ---------------------------------------------------------------- B-07 记忆回退


def test_memory_rollback_removes_chapter_events(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    w = MemoryWriter(ws, pid)
    w.append_plot_event({"id": "ev:1", "at": {"vol": 1, "ch": 1}, "type": "discovery",
                         "summary": "苏晚拾得断玉佩", "participants": ["char:sw"]})
    w.append_experience("char:sw", {"at": {"vol": 1, "ch": 1}, "summary": "苏晚拾得断玉佩"})
    w.append_plot_event({"id": "ev:2", "at": {"vol": 1, "ch": 2}, "type": "conflict",
                         "summary": "赵虎挑衅", "participants": ["char:zh"]})

    removed = rollback_chapter(ws, pid, 1, 1)
    assert removed["plot_events"] == 1 and removed["experiences"] == 1
    left = json.loads(ws._abs(f"{pid}/memory/plot_events.json").read_text(encoding="utf-8"))
    assert [e["summary"] for e in left] == ["赵虎挑衅"], "只回退指定章，其他章不受影响"
    idx = MemoryIndex.load(ws, pid)
    assert all(f.source != {"vol": 1, "ch": 1} for f in idx.fragments), "索引必须同步"


def test_memory_rollback_keeps_synthetic_chapter_event(tmp_path):
    """章级合成事件是进度记录，不应被情节回滚误删。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    w = MemoryWriter(ws, pid)
    w.append_plot_event({"id": "ev:syn", "at": {"vol": 1, "ch": 1}, "type": "chapter",
                         "summary": "完成第 1 卷第 1 章", "participants": []})
    w.append_plot_event({"id": "ev:real", "at": {"vol": 1, "ch": 1}, "type": "discovery",
                         "summary": "苏晚拾得断玉佩", "participants": ["char:sw"]})
    rollback_chapter(ws, pid, 1, 1)
    left = json.loads(ws._abs(f"{pid}/memory/plot_events.json").read_text(encoding="utf-8"))
    assert [e["type"] for e in left] == ["chapter"]


def test_memory_revise_fragment(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    w = MemoryWriter(ws, pid)
    w.append_plot_event({"id": "ev:1", "at": {"vol": 1, "ch": 1}, "type": "discovery",
                         "summary": "苏晚拾得断玉", "participants": ["char:sw"]})
    idx = MemoryIndex.load(ws, pid)
    sig = next(f.sig for f in idx.fragments if f.kind == "plot_event")
    w2 = MemoryWriter(ws, pid, index=idx)
    assert w2.revise_fragment(sig, "苏晚拾得断玉佩（修订）") is True
    assert any("修订" in f.text for f in MemoryIndex.load(ws, pid).fragments)


def test_memory_revise_missing_fragment_returns_false(tmp_path):
    ws, pid = _project(tmp_path)
    assert MemoryWriter(ws, pid).revise_fragment("nope:1.1:xxx", "x") is False


# ---------------------------------------------------------------- B-09 敏感词


def test_moderation_default_wordlist_is_not_empty():
    p = ModerationPrechecker()
    assert p.banned, "默认词表不得为空（原先为空导致上游预检形同虚设）"
    assert p.scan("他在交易海洛因。")


def test_moderation_empty_list_means_disabled():
    p = ModerationPrechecker(banned=[])
    assert p.banned == [] and p.scan("海洛因") == []


def test_moderation_check_action():
    p = ModerationPrechecker()
    assert p.check("正常正文。")["passed"] is True
    r = p.check("境外赌博")
    assert r["passed"] is False and r["action"] == "warn"
    assert ModerationPrechecker(block_on_hit=True).check("境外赌博")["action"] == "block"


def test_moderation_loads_from_file(tmp_path):
    f = tmp_path / "words.txt"
    f.write_text("# 注释\n老虎\n狮子\n", encoding="utf-8")
    assert load_banned_words(f) == ["老虎", "狮子"]
    j = tmp_path / "words.json"
    j.write_text(json.dumps({"banned_words": ["狼"]}), encoding="utf-8")
    assert load_banned_words(j) == ["狼"]


def test_moderation_from_workspace_uses_project_file(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    _write(ws, pid, "bible/moderation.json", {"banned_words": ["老虎"]})
    p = ModerationPrechecker.from_workspace(ws, pid)
    assert p.banned == ["老虎"]


# ---------------------------------------------------------------- B-13 导出书名


def test_export_uses_project_title_not_id(tmp_path):
    ws, pid = _project(tmp_path, title="断玉青冥")
    _seed(ws, pid)
    d = ws._abs(f"{pid}/chapters")
    d.mkdir(parents=True, exist_ok=True)
    (d / "1-1.md").write_text("正文。", encoding="utf-8")
    text = export_project(ws, pid)
    assert text.startswith("# 断玉青冥"), "书名应取 project.json.title"
    assert pid not in text.splitlines()[0]


def test_export_falls_back_to_project_id_without_title(tmp_path):
    ws, pid = _project(tmp_path)
    Checkpoint(ws).save(pid, {"id": pid, "pipeline_state": "正文"})
    assert export_project(ws, pid).startswith(f"# {pid}")



# ---------------------------------------------------------------- 规则引擎未被破坏


def test_existing_rules_still_work(tmp_path):
    ws, pid = _project(tmp_path)
    _write(ws, pid, "bible/characters.json", [
        {"id": "char:a", "name": "A", "relationships": [{"target": "char:missing", "type": "ally"}]}])
    assert any(a.rule_id == "R-REF" for a in run_rule_checks(ws, pid))


# ---------------------------------------------------------------- R-CAST 出场覆盖度（docs/05 检查员）


def _cast_project(tmp_path, pid="proj-cast"):
    """建档 3 人：苏晚/铁无涯 first_appear 1:1，赵虎 1:2（未来）。"""
    ws, pid = _project(tmp_path, pid=pid)
    _seed(ws, pid)
    d = ws._abs(f"{pid}/chapters")
    d.mkdir(parents=True, exist_ok=True)
    return ws, pid, d


def _cast_alerts(ws, pid):
    return [a for a in run_rule_checks(ws, pid) if a.rule_id == "R-CAST"]


def test_rule_cast_flags_planned_character_missing(tmp_path):
    ws, pid, d = _cast_project(tmp_path)
    (d / "1-1.md").write_text("苏晚负手而立，望着山门。", encoding="utf-8")
    alerts = _cast_alerts(ws, pid)
    # 铁无涯 first_appear 1:1 已越过且零出场 → 强信号；赵虎 1:2 在未来 → 不告警
    assert any("铁无涯" in a.detail and "计划在 1:1" in a.detail for a in alerts)
    assert not any("赵虎" in a.detail for a in alerts)


def test_rule_cast_ok_when_all_planned_appear(tmp_path):
    ws, pid, d = _cast_project(tmp_path)
    (d / "1-1.md").write_text("苏晚与铁无涯并肩而立，望着山门。", encoding="utf-8")
    assert _cast_alerts(ws, pid) == []


def test_rule_cast_weak_signal_without_first_appear(tmp_path):
    ws, pid = _project(tmp_path)
    _write(ws, pid, "bible/characters.json", [
        {"id": "char:sw", "name": "苏晚", "gender": "male", "status": "active"},
        {"id": "char:mystery", "name": "神秘人", "gender": "male", "status": "active"},
    ])
    d = ws._abs(f"{pid}/chapters")
    d.mkdir(parents=True, exist_ok=True)
    (d / "1-1.md").write_text("苏晚负手而立。", encoding="utf-8")
    alerts = _cast_alerts(ws, pid)
    assert any("神秘人" in a.detail and "未声明 first_appear" in a.detail for a in alerts)


def test_rule_cast_respects_aliases_and_skips_deceased(tmp_path):
    ws, pid, d = _cast_project(tmp_path)
    # 铁无涯别名「铁二爷」出场即算数；新增已死亡人物零出场不告警
    (d / "1-1.md").write_text("苏晚望着山门。铁二爷从旁走过。", encoding="utf-8")
    chars = [
        {"id": "char:sw", "name": "苏晚", "gender": "male", "status": "active",
         "core_traits": ["隐忍"], "power": {"level": "炼气三层", "faction": "青云宗"},
         "first_appear": {"vol": 1, "ch": 1}, "is_protagonist": True},
        {"id": "char:twy", "name": "铁无涯", "gender": "male", "status": "active",
         "power": {"level": "金丹初期", "faction": "青云宗"}, "first_appear": {"vol": 1, "ch": 1},
         "aliases": ["铁二爷"]},
        {"id": "char:ghost", "name": "亡者", "status": "deceased",
         "first_appear": {"vol": 1, "ch": 1}},
    ]
    _write(ws, pid, "bible/characters.json", chars)
    alerts = _cast_alerts(ws, pid)
    assert not any("铁无涯" in a.detail for a in alerts)
    assert not any("亡者" in a.detail for a in alerts)


def test_rule_cast_no_chapters_silent(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    assert _cast_alerts(ws, pid) == []
