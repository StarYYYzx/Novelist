"""prompt 结构化审计批次 2 的源码级守卫（依据 `docs/prompt组装结构审计-2026-09-12.md`）。

覆盖五组修复，每组一条（或数条）守卫防回退：

- **审校维度**：`REVIEW_PROMPT` 的维度名必须能过 `_parse` 的白名单（原「时间线与因果」
  不在 `CATEGORIES` → 静默归「其他」，实测 80 条真实 lessons 里「时间线」0 条）；
  补「伏笔」维度；旧措辞走别名收敛。
- **审校窗口**：章级走头+尾（原 `text[-3000:]` 丢章头）、细纲窗口与生成侧同源（原 800 vs 2400）、
  取证指引含当前章文件路径。
- **圣经摘要**：`_bible_brief` 补 glossary / 工艺卡 / 境界波动 / 现代词豁免。
- **机器字段键**：`humanize_kv` / `regroup_factions` 单一源，装配侧与读取侧共用。
- **Forge 侧**：system prompt 去内部文档坐标与章节号、细纲层补【世界观基座】、
  `context` 输出格式不再依赖 `L.pop()` 隐式假设。
"""

from __future__ import annotations

import inspect

import pytest

from novelist.consistency.reviewer import (
    CATEGORIES,
    CATEGORY_ALIASES,
    CHAPTER_REVIEW_CHARS,
    EVENT_REVIEW_CHARS,
    REVIEW_PROMPT,
    Reviewer,
    _bible_brief,
    review_context,
    review_window,
)
from novelist.consistency.reviewer_agent import REVIEWER_AGENT_SYSTEM, _evidence_pointer
from novelist.core.character_factory import _faction_names
from novelist.core.context import GIST_MAX_CHARS, build_system_prompt
from novelist.core.knowledge import QUERY_EV_CHARS, QUERY_EV_HEAD
from novelist.core.normalize import humanize_kv, regroup_factions, split_kv
from novelist.core.orchestrator import (
    SETTINGS_SUPPLEMENT_CHARS,
    SETTINGS_SUPPLEMENT_HEAD,
    _supplement_settings,
)
from novelist.core.prompt_budget import head_tail_window
from novelist.forge.nodes import NodeContext, _chapter_prompt, _volume_prompt
from novelist.forge.state import Blueprint

# ---------------------------------------------------------------- 夹具


def _bible(**style_extra) -> dict:
    st = {"tone": ["冷肃"], "pov": "第三人称限知"}
    st.update(style_extra)
    return {
        "worldview": {"name": "青冥界", "power_system": {"levels": ["炼气", "筑基"]},
                      "rules": ["修士不可对凡人出手"]},
        "style": st,
        "characters": [],
        "volumes": [],
    }


def _seed_bible(ws, pid, write_json) -> None:
    """落一套盘上 bible：含脏 factions、glossary（note + 旧 def 别名）、工艺卡、境界波动。"""
    write_json(ws, pid, "bible/worldview.json", {
        "id": "world:main", "name": "青冥界",
        "power_system": {"levels": ["炼气", "筑基"]},
        "rules": ["修士不可对凡人出手"],
        "realm_fluctuates": ["state：主角系统绑定后境界浮动"],
        "modern_words": ["internet：互联网"],
        # 逐字段拍平的脏数据（写侧修复前产生）：3 个字段被当成 3 条势力
        "factions": [{"faction": "name：玄剑宗"}, {"faction": "type：正道宗门"},
                     {"faction": "description：以剑修为主"},
                     {"faction": "name：天机阁"}, {"faction": "goals：推演天机"}],
        "civilizations": ["social_structure：以修士为核心的金字塔",
                          "cultural_norms：崇尚实力与名声"],
    })
    write_json(ws, pid, "bible/style.json", {
        "glossary": [{"term": "五五开", "note": "主角的金手指"},
                     {"term": "系统", "def": "旧字段别名"}],
        "craft_cards": ["chapter-rhythm"],
        "protagonist": {"name": "叶蓝", "gender": "male"},
    })
    write_json(ws, pid, "bible/characters.json", [
        {"id": "char:ye", "name": "叶蓝", "gender": "male",
         "power": {"level": "炼气"}, "core_traits": ["沉稳"]},
    ])


def _mk_bp(ws, pid):
    bp = Blueprint.blank({"title": "T"})
    bp.set("meta.scale", {"volumes": 1, "chapters_per_volume": 4})
    return bp


def _mk_ctx(ws, pid, bp, vol=1, ch=1):
    return NodeContext(ws=ws, project_id=pid, bp=bp, provider=None, pack={}, vol=vol, ch=ch)


# ---------------------------------------------------------------- 审校维度（P2-1 / P2-2）

def _dimension_lines() -> list[str]:
    """从 REVIEW_PROMPT 里只取【审校维度】小节的维度行（排除【输出】里的「- 级别：…」）。"""
    lines = REVIEW_PROMPT.splitlines()
    try:
        start = lines.index("【审校维度】")
        end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("【"))
    except (ValueError, StopIteration):
        pytest.fail("REVIEW_PROMPT 缺少【审校维度】小节结构")
    return [ln for ln in lines[start + 1:end] if ln.startswith("- ")]


def test_review_prompt_dimensions_all_parse_to_themselves():
    """守卫：`REVIEW_PROMPT` 里宣示的每个维度名都必须能被 `_parse` 原样映射回自己。

    原缺陷：「时间线与因果」不在 `CATEGORIES` → `_parse` 静默归「其他」，该维度
    在 80 条真实 lessons 里 0 次出现。本断言在修复前必然失败（即守卫生效的证明）。
    """
    dims = [ln.split("：", 1)[0].lstrip("- ").strip() for ln in _dimension_lines()]
    assert dims, "未从 REVIEW_PROMPT 解析出维度行"
    for dim in dims:
        issue = Reviewer._parse(object(), f"block | {dim} | 描述 | 建议")[0]
        assert issue.category == dim, f"维度「{dim}」被归为「{issue.category}」"


def test_review_prompt_declares_the_right_dimension_count():
    """「维度只能是上述 N 项之一」中的 N 必须与实际列出的维度数一致。"""
    dims = _dimension_lines()
    assert f"上述{_cn_num(len(dims))}项之一" in REVIEW_PROMPT


def _cn_num(n: int) -> str:
    return {7: "七", 8: "八", 9: "九"}.get(n, str(n))


def test_foreshadow_dimension_present_in_both_prompts():
    """P2-2：模块 docstring 宣称审「伏笔是否按预期推进」，但 prompt 里曾没有该维度。"""
    assert "伏笔" in REVIEW_PROMPT
    assert "伏笔" in REVIEWER_AGENT_SYSTEM


def test_agent_and_single_shot_prompts_agree_on_categories():
    """两条审校路径（单发 / 证据环）的维度表必须一致，否则互相漂移。"""
    line = next(ln for ln in REVIEWER_AGENT_SYSTEM.splitlines() if ln.startswith("- 维度仅限"))
    listed = {x.strip() for x in line.split("：", 1)[1].split("/")}
    assert listed == set(CATEGORIES)


def test_parse_converges_legacy_category_wording():
    """别名收敛：旧措辞（修复前 prompt 的写法）不再落进「其他」桶。"""
    assert "时间线与因果" in CATEGORY_ALIASES
    issue = Reviewer._parse(object(), "block | 时间线与因果 | 事件顺序乱 | 按序改")[0]
    assert issue.category == "时间线"
    assert issue.level == "block"


def test_parse_still_falls_back_to_other_for_unknown():
    """别名只是收敛，不是放宽——真正未登记的维度仍归「其他」。"""
    assert Reviewer._parse(object(), "warn | 莫名其妙 | 描述 | 建议")[0].category == "其他"


# ---------------------------------------------------------------- 审校窗口（P1-2 / P1-3）

def test_review_window_chapter_keeps_head_and_tail():
    head_tag, tail_tag = "【章头标记】", "【章尾标记】"
    text = head_tag + "甲" * (CHAPTER_REVIEW_CHARS + 4000) + tail_tag
    out = review_window(text, chapter_scope=True)
    assert head_tag in out, "章级必须保留章头（时间行/开篇事件密度最高）"
    assert tail_tag in out
    assert len(out) < len(text), "超预算时应折叠中段"


def test_review_window_event_is_tail_only():
    """事件级维持原尾部窗口（单事件片段通常远小于 3000，等价于不截断）。"""
    head_tag, tail_tag = "【片段头】", "【片段尾】"
    text = head_tag + "乙" * (EVENT_REVIEW_CHARS + 4000) + tail_tag
    out = review_window(text, chapter_scope=False)
    assert tail_tag in out and head_tag not in out


def test_review_context_gist_shares_generation_side_constant(monkeypatch):
    """审校细纲窗口必须与生成侧同源（原 800 vs 生成侧 2400，判定基础不对等）。"""
    monkeypatch.setattr("novelist.consistency.reviewer._bible_brief", lambda *a, **k: "（圣经）")
    assert GIST_MAX_CHARS == 2400
    gist = "标" * 2000 + "近端要点AAA" + "标" * 497 + "远端要点BBB" + "标" * 1000
    ctx = review_context(None, "x", gist_text=gist)  # ws 只在 _bible_brief 用；已 mock
    assert "近端要点AAA" in ctx
    assert "远端要点BBB" not in ctx


def test_review_context_gist_window_bypasses_bible_lookup(monkeypatch):
    """`review_context` 必须真的把 GIST_MAX_CHARS 用在 gist 上，而不是别的常量。"""
    monkeypatch.setattr("novelist.consistency.reviewer._bible_brief", lambda *a, **k: "（圣经）")
    gist = "甲" * GIST_MAX_CHARS + "切点后内容XYZ"
    assert "切点后内容XYZ" not in review_context(None, "x", gist_text=gist)


def test_review_chapter_file_reads_chapter_head(ws_factory, write_json):
    """端到端：`review_chapter_file` 把整章交给 review()，必须能看到章头（P1-2 原缺陷）。"""
    from tests.conftest import StubLLM

    ws, pid = ws_factory("proj-b2-window")
    _seed_bible(ws, pid, write_json)
    head_tag = "【这是章头唯一的标记】"
    chapter = head_tag + "丙" * (CHAPTER_REVIEW_CHARS + 3000) + "【章尾】"
    ws._abs(f"{pid}/chapters").mkdir(parents=True, exist_ok=True)
    ws._abs(f"{pid}/chapters/1-1.md").write_text(chapter, encoding="utf-8")

    stub = StubLLM("ok")
    Reviewer(ws, pid, llm=stub).review_chapter_file(1, 1)
    assert stub.calls, "审校未发出请求"
    assert head_tag in stub.calls[0], "章级审校仍未看到章头"


def test_evidence_pointer_includes_current_chapter_file(ws_factory):
    """P1-2：取证指引原本只给 bible/memory 路径，想补读章头无从下手。"""
    ws, pid = ws_factory("proj-b2-evptr")
    ws._abs(f"{pid}/chapters").mkdir(parents=True, exist_ok=True)
    ws._abs(f"{pid}/chapters/1-2.md").write_text("正文", encoding="utf-8")
    ptr = _evidence_pointer(ws, pid, 1, 2)
    assert "chapters/1-2.md" in ptr


# ---------------------------------------------------------------- 圣经摘要（P2-3）

def test_bible_brief_covers_glossary_craft_fluctuates_modern(ws_factory, write_json):
    ws, pid = ws_factory("proj-b2-brief")
    _seed_bible(ws, pid, write_json)
    brief = _bible_brief(ws, pid)
    assert "专有名词" in brief and "五五开" in brief
    assert "旧字段别名" in brief, "schema 旧字段 def 应被当作 note 读出"
    assert "境界浮动" in brief
    assert "互联网" in brief
    assert "题材工艺规范" in brief
    # 英文键不得进 summary
    assert "state：" not in brief and "internet：" not in brief


# ---------------------------------------------------------------- 机器字段键归一

def test_split_kv_only_matches_ascii_keys():
    assert split_kv("name：玄剑宗") == ("name", "玄剑宗")
    assert split_kv("social_structure: 以修士为核心") == ("social_structure", "以修士为核心")
    assert split_kv("社会结构：以修士为核心") is None, "中文键不是机器噪声"
    assert split_kv("这是一句普通的话") is None
    assert split_kv(None) is None


def test_humanize_kv_maps_known_and_strips_unknown():
    assert humanize_kv("social_structure：以修士为核心") == "社会结构：以修士为核心"
    assert humanize_kv("unknown_key：值") == "值", "未知英文键降级为只留值"
    assert humanize_kv("不认识的键：值") == "不认识的键：值", "中文键有语义，不当噪声剥"
    assert humanize_kv("社会结构：以修士为核心") == "社会结构：以修士为核心"
    assert humanize_kv("普通句子") == "普通句子"


def test_regroup_factions_rebuilds_objects_from_flattened_fields():
    flat = [{"faction": "name：玄剑宗"}, {"faction": "type：正道宗门"},
            {"faction": "description：以剑修为主"}, {"faction": "name：天机阁"},
            {"faction": "goals：推演天机"}]
    out = regroup_factions(flat)
    assert [f["faction"] for f in out] == ["玄剑宗", "天机阁"]
    assert out[0]["note"] == "类型：正道宗门；描述：以剑修为主"
    assert out[1]["note"] == "目标：推演天机"


def test_regroup_factions_plain_data_behaviour_unchanged():
    """非拍平数据必须与原实现逐字一致（str → {"faction": s}；dict 原样；无 faction 的丢弃）。"""
    items = ["华夏修行者协会", {"faction": "暗影组织", "note": "x"}, {"name": "无名"}]
    out = regroup_factions(items)
    assert out == [{"faction": "华夏修行者协会"}, {"faction": "暗影组织", "note": "x"}]


def test_regroup_factions_accepts_none():
    assert regroup_factions(None) == []
    assert regroup_factions([]) == []


def test_faction_names_are_clean_after_regroup(ws_factory, write_json):
    """`power.faction` 的取值域必须是干净势力名（原为 `name：玄剑宗` 之类垃圾）。"""
    ws, pid = ws_factory("proj-b2-faction")
    _seed_bible(ws, pid, write_json)
    assert _faction_names(ws, pid) == {"玄剑宗", "天机阁"}


def test_context_renders_worldview_without_english_keys():
    bible = _bible()
    bible["worldview"]["civilizations"] = ["social_structure：以修士为核心的金字塔"]
    bible["worldview"]["systems"] = ["level：炼气→筑基"]
    sp = build_system_prompt(bible, [], 1, 1)
    assert "social_structure" not in sp
    assert "社会结构：以修士为核心的金字塔" in sp


# ---------------------------------------------------------------- context 输出格式（P2-6）

def test_output_format_is_single_and_mode_correct():
    """`L.pop()` 隐式假设改为显式选值：任何模式下【输出格式】只能出现一次，且内容与模式匹配。"""
    bible = _bible()
    ev = build_system_prompt(bible, [], 1, 2, event_loop=True)
    assert ev.count("【输出格式】") == 1
    assert "不要写章节标题" in ev and "第一行是章节标题" not in ev

    plain = build_system_prompt(bible, [], 1, 2, event_loop=False)
    assert plain.count("【输出格式】") == 1
    assert "第一行是章节标题" in plain and "不要写章节标题" not in plain


# ---------------------------------------------------------------- Forge 侧（P1-1 / P2-9）

def test_forge_prompt_systems_have_no_doc_coordinates_or_chapter_number(ws_factory):
    """system prompt 里的 `docs/10 §7.x` 对模型无意义，是纯 token 噪声。"""
    import novelist.forge.nodes as nodes

    fns = [v for k, v in vars(nodes).items()
           if k.endswith("_prompt") and inspect.isfunction(v) and v.__module__ == nodes.__name__]
    assert fns, "未找到 forge prompt 函数"
    for fn in fns:
        src = inspect.getsource(fn)
        bad = [ln.strip() for ln in src.splitlines() if "return" in ln and "docs/" in ln]
        assert not bad, f"{fn.__name__} 的 system prompt 仍含文档坐标：{bad}"


def test_chapter_prompt_system_is_chapter_independent(ws_factory, write_json):
    ws, pid = ws_factory("proj-b2-sysstable")
    bp = _mk_bp(ws, pid)
    s1 = _chapter_prompt(_mk_ctx(ws, pid, bp, 1, 1))[0]
    s2 = _chapter_prompt(_mk_ctx(ws, pid, bp, 2, 5))[0]
    assert s1 == s2, "forge system prompt 随章节变会破坏缓存前缀"
    assert "docs/" not in s1
    v1 = _volume_prompt(_mk_ctx(ws, pid, bp, 1, 0))[0]
    v2 = _volume_prompt(_mk_ctx(ws, pid, bp, 3, 0))[0]
    assert v1 == v2


def test_chapter_prompt_injects_worldview_base(ws_factory):
    """P1-1：细纲层原先看不到境界体系与铁律（fame5 修为线不一致的成因之一）。"""
    ws, pid = ws_factory("proj-b2-wv")
    bp = _mk_bp(ws, pid)
    bp.set("worldview", {"name": "青冥界",
                         "power_system": {"levels": ["炼气", "筑基", "金丹"]},
                         "rules": ["修士不可对凡人出手"]})
    _, user = _chapter_prompt(_mk_ctx(ws, pid, bp))
    assert "【世界观基座】" in user
    assert "炼气、筑基、金丹" in user
    assert "修士不可对凡人出手" in user
    assert "青冥界" in user


def test_chapter_prompt_without_worldview_keeps_original_layout(ws_factory):
    """无世界观时不得多出空行或空标题——注入必须是纯增量。"""
    ws, pid = ws_factory("proj-b2-wv-empty")
    bp = _mk_bp(ws, pid)
    bp.set("worldview", {})
    _, user = _chapter_prompt(_mk_ctx(ws, pid, bp))
    assert "【世界观基座】" not in user
    assert "章/卷）。\n\n【本卷主线】" in user


def test_chapter_prompt_worldview_is_pure_addition(ws_factory):
    """有无世界观的两版 prompt，除新增块外必须逐字相同（证明不是重排）。"""
    ws, pid = ws_factory("proj-b2-wv-diff")
    bp = _mk_bp(ws, pid)
    bp.set("worldview", {})
    before = _chapter_prompt(_mk_ctx(ws, pid, bp))[1]
    bp.set("worldview", {"power_system": {"levels": ["炼气"]}})
    after = _chapter_prompt(_mk_ctx(ws, pid, bp))[1]
    marker = "【本卷主线】"
    assert after.endswith(before[before.index(marker):]), "除新增块外不得重排既有内容"
    assert "【世界观基座】" in after


# ---------------------------------------------------------------- 窗口助手

def test_head_tail_window_is_noop_below_budget():
    assert head_tail_window("短文本", 100) == "短文本"


def test_head_tail_window_folds_middle_and_keeps_ends():
    text = "头" * 50 + "中" * 500 + "尾" * 50
    out = head_tail_window(text, 120, head_chars=40)
    assert out.startswith("头")
    assert out.endswith("尾")
    assert "中段略" in out


# ---------------------------------------------------------------- 检索 / 提案窗口（P2-4 / P2-5）

def test_plan_queries_window_sees_head_and_tail():
    from tests.conftest import StubLLM

    from novelist.core.knowledge import KnowledgeBase

    head_tag, tail_tag = "【事件头】", "【事件尾】"
    ev = head_tag + "丁" * 3000 + tail_tag
    stub = StubLLM("查询一,查询二")
    obj = KnowledgeBase.__new__(KnowledgeBase)  # 只测窗口，不依赖索引装配
    obj.plan_queries(ev, provider=stub)
    assert stub.calls, "未发出检索规划请求"
    assert head_tag in stub.calls[0] and tail_tag in stub.calls[0]


def test_supplement_settings_window_sees_head(ws_factory, write_json):
    from tests.conftest import StubLLM

    ws, pid = ws_factory("proj-b2-supp")
    _seed_bible(ws, pid, write_json)
    head_tag = "【新地点首现】"
    ev = head_tag + "戊" * (SETTINGS_SUPPLEMENT_CHARS + 2000) + "尾部"
    stub = StubLLM("[]")
    assert _supplement_settings(ws, pid, ev, stub) == 0
    assert stub.calls and head_tag in stub.calls[0], "提案器仍看不到事件开场"


def test_window_constants_are_generous_enough():
    """回归护栏：这批常量若被改回原值（200 / 800），说明修复被回退。"""
    assert QUERY_EV_CHARS > 200 and QUERY_EV_HEAD > 0
    assert SETTINGS_SUPPLEMENT_CHARS > 800 and SETTINGS_SUPPLEMENT_HEAD > 0
    assert CHAPTER_REVIEW_CHARS > EVENT_REVIEW_CHARS


@pytest.mark.parametrize("name", ["QUERY_EV_CHARS", "SETTINGS_SUPPLEMENT_CHARS"])
def test_window_constants_exist(name):
    import novelist.core.knowledge as K
    import novelist.core.orchestrator as O

    assert hasattr(K, name) or hasattr(O, name)
