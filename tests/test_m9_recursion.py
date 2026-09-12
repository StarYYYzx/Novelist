"""递归分层 + 质量增强测试（第七批讨论落地，M3j/M3k）。

覆盖：
- 三缺陷修复：R-STATE 绑定豁免 / 编纂抽取能力词过滤 / R-LEX 穿越者豁免
- 事件级润色 + 风格 tone 驱动（TONE_TEMPLATES）
- 回读机制（_prior_chapter_text 注入前章原文）
- 递归分层 A：人物 JIT 补卡（细纲声明 → bible 缺卡 → LLM 补全）
- 递归分层 B：世界观滚动补充（_supplement_settings）
- 递归分层 C：重场戏拍展开（[expanded] → ≤3 拍）
"""

from __future__ import annotations

import json

import pytest

from novelist.consistency import run_consistency
from novelist.consistency.rules import run_lexicon_checks, run_state_checks
from novelist.core.llm import LLMResult
from novelist.core.orchestrator import (
    _prior_chapter_text,
    _supplement_settings,
    is_expanded_event,
    parse_cast_decl,
)
from novelist.core.polish import TONE_TEMPLATES, build_polish_prompt, measure
from novelist.core.session import SessionInfo
from novelist.storage.checkpoint import Checkpoint
from novelist.storage.workspace import Workspace


def _project(tmp_path) -> tuple[Workspace, str]:
    ws = Workspace(root=str(tmp_path))
    pid = "proj-t"
    ws.create_project(pid)
    Checkpoint(ws).save(pid, {"id": pid, "pipeline_state": "正文"})
    return ws, pid


def _write(ws, pid, rel, data):
    ws.write_json(ws._abs(f"{pid}/{rel}"), data)


def _seed(ws, pid):
    _write(ws, pid, "bible/characters.json", [
        {"id": "char:zhu", "name": "叶岚", "gender": "male", "is_protagonist": True,
         "core_traits": ["稳健"], "power": {"level": "炼气三层"},
         "first_appear": {"vol": 1, "ch": 1}},
        {"id": "char:qing", "name": "沈青梧", "gender": "female",
         "core_traits": ["飒爽"], "power": {"level": "筑基中期"},
         "first_appear": {"vol": 1, "ch": 2}},
    ])
    _write(ws, pid, "bible/worldview.json",
           {"name": "青云界", "power_system": {"levels": ["炼气", "筑基", "金丹"]},
            "modern_words": ["程序员", "手机"]})
    _write(ws, pid, "bible/style.json",
           {"protagonist": {"id": "char:zhu", "name": "叶岚", "gender": "male"},
            "forbidden_words": ["打卡"], "tone": "严谨冷肃"})
    _write(ws, pid, "bible/plot_threads.json", [
        {"id": "thread:yubi", "status": "planted", "desc": "断玉佩的来历与封印"},
    ])


def _draft(ws, pid, ch, text):
    p = ws.draft_path(pid, 1, ch)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- 三缺陷修复

def test_rstate_bind_exempt(tmp_path):
    """R-STATE × 绑定流（P0-1）：「借用他人境界」不得判倒退。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    _write(ws, pid, "bible/worldstate.json", {"characters": {
        "char:zhu": {"name": "叶岚", "realm": "炼气三层", "location": "", "items": [],
                     "injuries": [], "dead": False, "history": [
                         {"at": {"vol": 1, "ch": 2}, "delta": {"realm": "筑基中期（借用沈青梧境界）"}},
                         {"at": {"vol": 1, "ch": 3}, "delta": {"realm": "炼气三层"}},
                     ]}}}),
    alerts = [a for a in run_state_checks(ws, pid) if a.rule_id == "R-STATE"]
    assert not alerts, f"绑定流不应判倒退：{alerts}"


def test_lexicon_exempt_modern_words(tmp_path):
    """R-LEX × 穿越者（P1-1）：modern_words_exempt 豁免后不告警。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    _write(ws, pid, "bible/worldview.json",
           {"name": "青云界", "power_system": {"levels": ["炼气", "筑基", "金丹"]},
            "modern_words": ["程序员", "手机"], "modern_words_exempt": ["程序员"]})
    _draft(ws, pid, 1, "## 第一章\n\n叶岚凭借前世作为程序员的直觉，用手机传讯，一眼看出阵法破绽。")
    alerts = [a for a in run_lexicon_checks(ws, pid) if "现代词汇「程序员」" in a.detail]
    assert not alerts, f"豁免后不应告警：{alerts}"
    # 未豁免的手机仍应告警
    assert any("现代词汇「手机」" in a.detail for a in run_lexicon_checks(ws, pid))


def test_chronicler_abstract_item_filtered(tmp_path):
    """编纂能力词过滤（P0-2）：能力/权限/警告信息不是物品，不进 items。"""
    from novelist.core.worldstate import apply_delta

    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    _write(ws, pid, "bible/items.json", [])
    _write(ws, pid, "bible/skills.json", [])
    apply_delta(ws, pid, "char:zhu",
                {"获得": "系统绑定权、云清瑶力量、警告信息、洗髓丹"}, {"vol": 1, "ch": 1})
    st = json.loads(ws._abs(f"{pid}/bible/worldstate.json").read_text(encoding="utf-8"))
    items = st["characters"]["char:zhu"]["items"]
    assert "洗髓丹" in items
    assert not any("力量" in it or "权" in it or "警告" in it for it in items), items


# ---------------------------------------------------------------- 事件级润色 + tone

def test_tone_templates_available():
    """风格 skill（第七批第 2 条）：四套模板齐全，至少覆盖严谨冷肃/诙谐幽默。"""
    assert "严谨冷肃" in TONE_TEMPLATES and "诙谐幽默" in TONE_TEMPLATES
    assert len(TONE_TEMPLATES) >= 4


def test_build_polish_prompt_tone():
    """tone 驱动：指定风格后 prompt 含对应模板；事件级不带章节标题指令。"""
    text = "那一刻，他仿佛看到了什么。"
    p = build_polish_prompt(text, tone="诙谐幽默", is_chapter=False)
    assert "诙谐幽默" in p
    assert "不要加标题" in p
    assert "第一行是章节标题" not in p
    # AI 味信号统计进了 prompt
    assert "这一刻" in p


# ---------------------------------------------------------------- 回读机制

def test_prior_chapter_text_injects_original(tmp_path):
    """回读机制（第七批第 4 条）：注入前 1 章正文**原文**，不是摘要。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    _draft(ws, pid, 1, "## 第一章\n\n赵铁山被问话时手抖了一下。")  # 细节：手抖
    text = _prior_chapter_text(ws, pid, 1, 2)
    assert "赵铁山" in text and "手抖" in text, "回读应带原文细节"
    # ch1 没有前章 → 空
    assert _prior_chapter_text(ws, pid, 1, 1) == ""


# ---------------------------------------------------------------- 递归分层 A：人物 JIT

class _StubLLM:
    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls = []

    def complete(self, req):
        self.calls.append(req.messages[-1].content[:60])
        return LLMResult(ok=True, content=self.responses.pop(0), finish_reason="stop",
                         provider="stub")


def test_parse_cast_decl():
    assert parse_cast_decl("出场人物: [赵铁山, 鬼面]\nkey_events: [x]") == ["赵铁山", "鬼面"]
    assert parse_cast_decl("cast: [沙无咎]") == ["沙无咎"]
    assert parse_cast_decl("key_events: [x]") == []


def test_jit_characters_fills_missing(tmp_path):
    """递归分层 A（ADR-022 后 JIT 降级为告警器）：细纲声明出场但缺卡 →
    不再 LLM 补卡写卡（追认通道关死，与 P0-B 合围），缺名转角色工厂需求队列。"""
    from novelist.core.character_factory import load_queue
    from novelist.core.orchestrator import _jit_characters

    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    llm = _StubLLM([json.dumps([{
        "name": "赵铁山", "gender": "male", "age": 21, "species": "人族",
        "core_traits": ["憨直", "讲义气"], "power": {"level": "炼气二层", "faction": "青云宗"},
        "arc": "从室友到生死之交", "first_appear": {"vol": 1, "ch": 2},
    }], ensure_ascii=False)])
    n = _jit_characters(ws, pid, 1, 2,
                        "出场人物: [赵铁山]\nkey_events: [室友帮叶岚带早饭]",
                        llm)
    assert n == 1  # 入队数 = 缺卡数
    chars = json.loads(ws._abs(f"{pid}/bible/characters.json").read_text(encoding="utf-8"))
    names = {c["name"] for c in chars}
    assert "赵铁山" not in names  # 卡未被追认写档（留给工厂正面登记）
    needs = load_queue(ws, pid)
    assert len(needs) == 1 and needs[0].source == "jit_alarm"
    assert "赵铁山" in needs[0].description and needs[0].vol == 1 and needs[0].ch == 2
    # 再次调用（缺卡需求已在队）→ 幂等，不重复入队
    assert _jit_characters(ws, pid, 1, 3, "出场人物: [赵铁山]", llm) == 0
    assert len(load_queue(ws, pid)) == 1


# ---------------------------------------------------------------- 递归分层 B：世界观补充

def test_supplement_settings_new_terms_go_pending(tmp_path):
    """递归分层 B（P0-B 闸门化）：事件新名词 → 进 settings_pending.json 待人工确认，
    不再自动入档 settings.json（"强化符"反向追认通道关死）。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    _write(ws, pid, "bible/settings.json", [])
    llm = _StubLLM([json.dumps([{
        "term": "血月之夜", "text": "每六十年一次的诡异异变之夜",
        "keywords": ["血月", "异变"],
    }], ensure_ascii=False)])
    n = _supplement_settings(ws, pid, "血月之夜降临，宗门大阵震动", llm)
    assert n == 1
    entries = json.loads(ws._abs(f"{pid}/bible/settings.json").read_text(encoding="utf-8"))
    assert entries == []  # 闸门：绝不自动入档
    pending = json.loads(ws._abs(f"{pid}/bible/settings_pending.json").read_text(encoding="utf-8"))
    assert pending[0]["term"] == "血月之夜" and "待人工确认" in pending[0]["reason"]
    # 已知词（叶岚/沈青梧）不会被重复提取——第二次调用传已知名词应返回 0
    llm2 = _StubLLM(["[]"])
    assert _supplement_settings(ws, pid, "叶岚与沈青梧交谈", llm2) == 0


# ---------------------------------------------------------------- 递归分层 C：拍展开

def test_is_expanded_event():
    assert is_expanded_event("大比夺魁 [expanded]")
    assert not is_expanded_event("大比夺魁")


def test_generate_beats_three_beats(tmp_path):
    """递归分层 C：事件 [expanded] → 拆 3 拍逐拍生成，带上一拍全文。"""
    from novelist.core.orchestrator import _generate_beats

    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    plan = "1. 登场：赵铁山向叶岚下战书\n2. 交锋：擂台对轰\n3. 收束：叶岚险胜"
    llm = _StubLLM([
        plan,
        "叶岚踏上擂台，目光扫过台下。赵铁山抱拳而立，气势凌厉。",
        "拳风交错的瞬间，叶岚侧身避开，反手一记灵掌拍在赵铁山肩头。",
        "赵铁山踉跄退了三步，最终抱拳认输。叶岚赢得干净利落。",
    ])
    res = _generate_beats(llm, "system", "goal", "大比夺魁 [expanded]",
                          memories=["先忆"], setting_lines=[], related={},
                          readback_text="", generation_tokens=200,
                          max_continuations=1, direct_words_floor=5)
    assert res is not None
    text, beats = res
    assert beats == 3
    assert "擂台" in text and "认输" in text
    # 拆解失败 → 回退事件级（None）
    llm2 = _StubLLM(["不是拍清单"])
    assert _generate_beats(llm2, "system", "goal", "普通事件", [], [], {}, "",
                           200, 1, 5) is None


# ---------------------------------------------------------------- 端到端：produce_chapter 全开关

def test_produce_chapter_with_recursion_switches(tmp_path):
    """produce_chapter 全开关冒烟：JIT+事件循环+拍展开+回读+设定补充。"""
    from novelist.core.orchestrator import produce_chapter

    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    _draft(ws, pid, 1, "## 第一章\n\n叶岚在客栈打坐，窗外飘过血月之光。")
    g = ws.outline_chapter_path(pid, 1, 2)
    g.parent.mkdir(parents=True, exist_ok=True)
    g.write_text("出场人物: [赵铁山]\nkey_events: [赵铁山挑战叶岚 [expanded], "
                 "叶岚答应比试]\n", encoding="utf-8")
    # 序列：JIT 补卡 → 拍1 → 拍2 → 拍3 → 审校(3×) → 编纂(3×) → 事件2 → 审校 → 编纂
    # 简化断言：只验证主链路可跑通、不崩溃（JIT 已单独测）
    res = produce_chapter(
        ws, pid, 1, 2, _StubLLM([
            # JIT 补卡
            json.dumps([{"name": "赵铁山", "gender": "male", "core_traits": ["憨直"],
                         "power": {"level": "炼气二层"}}], ensure_ascii=False),
            # 拍展开：plan
            "1. 挑战\n2. 交手\n3. 定局",
            # 拍1-3
            "赵铁山在演武场叫阵，声震全场。",
            "两人交手，叶岚以五五开系统借力，堪堪平分秋色。",
            "赵铁山力竭认输，叶岚抱拳。",
            # 事件1审校 → 事件1编纂 → 事件2生成 → 事件2审校 → 事件2编纂
            "ok",
            "赵铁山挑战 | conflict | 叶岚",
            "叶岚答应三日后正式比试。",
            "ok",
            "叶岚答应 | dialogue | 叶岚",
        ]),
        prefer_direct=True, inject_bible=False, event_loop=True,
        commit_chapter_event=False, direct_words_floor=5, knowledge_llm=False,
        jit_characters=True, supplement_settings=False,
        event_review=True, event_polish=False, readback=False,
        character_direction=False, perspective_memory=False, defer_title=False,
        broadcast_casting=False, session=SessionInfo(project_id=pid, agent="t"))
    assert res.ok, res.result
    final = ws.draft_path(pid, 1, 2).read_text(encoding="utf-8")
    assert "赵铁山" in final


# ---------------------------------------------------------------- 接缝复述去重（v5 ch1 实测 bug）

def test_strip_seam_overlap_removes_repetition():
    """接缝复述去重：模型把接缝复述一遍再续写（跨 v2→v5 老 bug）。"""
    from novelist.core.orchestrator import strip_seam_overlap

    prev = ("就在这时，门外传来粗重的脚步声。几个杂役弟子压低声音交谈："
            "“听说内门那位云师姐今日路过柴房附近，陈师兄可是专门去‘拜访’了。”\n\n"
            "叶岚瞳孔微缩。现在还不能暴露自己已经觉醒系统的事实。"
            "他深吸一口气，将悸压了下去。")
    piece = ("“听说内门那位云师姐今日路过柴房附近，陈师兄可是专门去‘拜访’了。”\n\n"
             "叶岚瞳孔微缩。云清瑶站在柴房门口，周身灵气凝而不散。\n\n"
             "不过现在，他还不能暴露自己已经觉醒系统的事实。他推门而出。")
    out = strip_seam_overlap(prev, piece)
    # 纯重复对白被删；新信息保留
    assert "师兄可是专门去" not in out
    assert "柴房门口" in out
    assert "不能暴露自己已经觉醒" not in out  # 复述句（prev 有同义句）被删
    assert "推门而出" in out  # 新内容保留


def test_strip_seam_overlap_keeps_normal_text():
    """无重复的正常续写不受影响（防误删）。"""
    from novelist.core.orchestrator import strip_seam_overlap

    prev = "叶岚在柴房里打坐，窗外的月光渐渐西斜。"
    piece = "他推门而出，院子里月光如水。远处的藏经阁灯火通明，似乎今夜有人值守。"
    out = strip_seam_overlap(prev, piece)
    assert "藏经阁" in out and "推门而出" in out
    assert len(out) >= len(piece.strip()) * 0.8


def test_ngram_sim_coverage_not_jaccard():
    """覆盖率而非 Jaccard：短句对比长上文不被稀释。"""
    from novelist.core.orchestrator import _ngram_sim

    sent = "不过现在，他还不能暴露自己已经觉醒系统的事实。"
    long_prev = "叶岚瞳孔微缩。" * 5 + "现在还不能暴露自己已经觉醒系统的事实。" + "他深吸一口气。" * 5
    assert _ngram_sim(sent, long_prev) > 0.6  # 覆盖率高
    unrelated = "窗外下起了大雨，雨点敲打着屋檐。"
    assert _ngram_sim(unrelated, long_prev) < 0.2
