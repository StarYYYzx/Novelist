"""prompt 审计批次 1 的源码级守卫（依据 `docs/prompt审计-2026-09-12.md`）。

四项零风险修复各配一条守卫，防止回退：

- **P0-1** system prompt 前缀不随章节变（DeepSeek 缓存按「从第 0 个 token 起前缀一致」命中）
  + `Usage` 采集缓存拆分 + 审计报告输出命中率
- **P0-2** 标题指令不再同场矛盾（事件模式：纪律与输出格式必须一致）
- **P0-3 / P0-4** 禁令表去重、术语表归一（拆「A、B、C」式 term + 同 term 归并 + 空 note 不渲染空括号），
  且归一逻辑单一源（`core/normalize.py`）被装配侧与 Forge 写入侧共用
- **P1-5** `_seam_retell` 补 `response_format`（此前靠正则抠 JSON）

源码级守卫（而非 end-to-end）的理由：这些是**装配契约**，纯函数即可判定，不需要真机。
"""

from __future__ import annotations

import json

import pytest

from novelist.core.context import build_system_prompt
from novelist.core.llm import LLMResult, Usage, estimate_cost
from novelist.core.normalize import dedup_keep_order, normalize_glossary, split_terms
from novelist.core.orchestrator import _seam_retell, _UsageCounter, _write_generation_audit
from novelist.forge.nodes import _coerce_str_list
from novelist.providers.openai import parse_completion


def _bible(**style_extra) -> dict:
    st = {"tone": ["冷肃"], "pov": "第三人称限知"}
    st.update(style_extra)
    return {
        "worldview": {
            "name": "青冥界",
            "power_system": {"levels": ["炼气", "筑基"]},
            "rules": ["修士不可对凡人出手"],
        },
        "style": st,
        "characters": [],
        "volumes": [],
    }


# ---------------------------------------------------------------- P0-1 缓存前缀

def test_system_prompt_stable_across_chapters():
    """同一 bible+cast 下 system prompt 必须逐字节稳定——否则每章前缀全变，缓存结构性 0 命中。"""
    bible = _bible()
    assert build_system_prompt(bible, [], 1, 2) == build_system_prompt(bible, [], 2, 7)


def test_system_prompt_has_no_chapter_number():
    """章节号只应出现在 user_goal，不该回到 system prompt 第 0 行（P0-1 的原始缺陷）。"""
    sp = build_system_prompt(_bible(), [], 1, 3)
    assert "正在写第" not in sp
    assert "第 1 卷第 3 章" not in sp


def test_parse_completion_collects_cache_split():
    data = {
        "choices": [{"message": {"content": "x"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 50,
                  "prompt_cache_hit_tokens": 900, "prompt_cache_miss_tokens": 100},
    }
    res = parse_completion(200, data)
    assert res.usage.cache_hit_tokens == 900
    assert res.usage.cache_miss_tokens == 100


def test_parse_completion_cache_fields_none_when_unreported():
    """未上报 ≠ 上报 0：不可测时必须为 None，不能伪造成 0（否则命中率显示成 0% 骗人）。"""
    data = {
        "choices": [{"message": {"content": "x"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 50},
    }
    res = parse_completion(200, data)
    assert res.usage.cache_hit_tokens is None
    assert res.usage.cache_miss_tokens is None


def test_estimate_cost_splits_cache_price():
    """命中价与未命中价必须分开算——旧口径按单一 in 价计，看不见缓存收益。"""
    hit = estimate_cost(cache_hit=1_000_000)
    miss = estimate_cost(cache_miss=1_000_000)
    out = estimate_cost(tokens_out=1_000_000)
    assert hit > 0
    assert miss == pytest.approx(hit * 50)  # 0.02 vs 1.0 元/百万
    assert out > 0


def test_generation_audit_reports_cache_hit_rate(ws_factory):
    ws, pid = ws_factory()
    counter = _UsageCounter(provider=None)
    counter.calls, counter.tokens_in, counter.tokens_out = 3, 1000, 100
    counter.cache_hit_tokens, counter.cache_miss_tokens = 900, 100
    counter.cache_seen = True
    _write_generation_audit(ws, pid, 1, 1, counter, ok=True)
    gens = sorted(ws._abs(f"{pid}/reports/stats").glob("generation-*.md"))  # noqa: SLF001
    assert gens, "应写正文生成报告"
    text = gens[-1].read_text(encoding="utf-8")
    assert "命中率 90.0%" in text


def test_generation_audit_flags_untracked_cache(ws_factory):
    """未上报缓存拆分的 provider（fake 等）报告要明说「不可测」，不写 0%。"""
    ws, pid = ws_factory()
    counter = _UsageCounter(provider=None)
    counter.calls, counter.tokens_in, counter.tokens_out = 1, 500, 20
    _write_generation_audit(ws, pid, 1, 1, counter, ok=True)
    gens = sorted(ws._abs(f"{pid}/reports/stats").glob("generation-*.md"))  # noqa: SLF001
    text = gens[-1].read_text(encoding="utf-8")
    assert "缓存命中率不可测" in text


def test_generation_audit_payload_has_cache_fields(ws_factory):
    ws, pid = ws_factory()
    counter = _UsageCounter(provider=None)
    counter.calls, counter.tokens_in, counter.tokens_out = 1, 500, 20
    counter.cache_hit_tokens, counter.cache_miss_tokens = 400, 100
    counter.cache_seen = True
    _write_generation_audit(ws, pid, 1, 2, counter, ok=True)

    from novelist.storage.indexdb import IndexDb

    db = IndexDb(str(ws.index_db_path(pid)))
    db.init()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT payload FROM audit_log WHERE kind=? ORDER BY seq", ("produce_chapter",)
        ).fetchall()
    payload = json.loads(rows[-1][0])
    assert payload["cache_hit_tokens"] == 400
    assert payload["cache_miss_tokens"] == 100


# ---------------------------------------------------------------- P0-2 标题指令

def test_title_instruction_not_contradictory():
    """事件模式下「纪律说写标题」与「输出格式说不写」不得并存（H8 只修了一半的残留）。"""
    bible = _bible()
    event = build_system_prompt(bible, [], 1, 3, event_loop=True)
    direct = build_system_prompt(bible, [], 1, 3, event_loop=False)
    assert "不要写章节标题" in event
    assert "章节标题只在第一行出现一次" not in event
    assert "第一行是章节标题" in direct
    assert "不要写章节标题" not in direct


# ---------------------------------------------------------------- P0-3 / P0-4 数据侧去重

def _section_line(sp: str, prefix: str) -> str:
    return next(ln for ln in sp.splitlines() if ln.startswith(prefix))


def test_forbidden_words_deduped_in_prompt():
    """禁令表渲染前去重（forge 生成侧实测产出精确重复）。"""
    bible = _bible(forbidden_words=[
        "毫无疑问", "毫无疑问地", "毫无疑问的", "毫无疑问地", "毫无疑问的", "  ", "",
    ])
    line = _section_line(build_system_prompt(bible, [], 1, 3), "- 禁用词")
    words = [w.strip() for w in line.split("：", 1)[1].split("、")]
    assert words == ["毫无疑问", "毫无疑问地", "毫无疑问的"]


def test_glossary_merged_and_empty_note_has_no_parens():
    """同 term 只渲染一次（note 取更详尽者）；note 为空不渲染空括号。"""
    bible = _bible(glossary=[
        {"term": "声望值", "note": ""},
        {"term": "明善暗恶", "note": ""},
        {"term": "声望值", "note": "系统核心货币"},
        {"term": "【出名就变强系统】", "note": ""},
    ])
    body = _section_line(build_system_prompt(bible, [], 1, 3), "- 专有名词").split("：", 1)[1]
    assert body.count("声望值") == 1
    assert "声望值（系统核心货币）" in body
    assert "（）" not in body
    assert "、明善暗恶、" in body


def test_glossary_tolerates_malformed_entries():
    """非 dict / 无 term 的脏数据不得渲染出空条目。"""
    bible = _bible(glossary=["不是字典", {"note": "无 term"}, {"term": "  ", "note": "x"},
                             {"term": "有效词", "note": ""}])
    body = _section_line(build_system_prompt(bible, [], 1, 3), "- 专有名词").split("：", 1)[1]
    assert body == "有效词"


def test_glossary_splits_list_term():
    """顿号清单式 term 拆成原子术语，并与详细条目归并（实测 4/56 条命中此形态）。"""
    bible = _bible(glossary=[
        {"term": "【出名就变强系统】、声望值、明善暗恶", "note": ""},   # forge 实测的脏写法
        {"term": "声望值", "note": "系统核心货币"},
    ])
    body = _section_line(build_system_prompt(bible, [], 1, 3), "- 专有名词").split("：", 1)[1]
    assert body == "【出名就变强系统】、声望值（系统核心货币）、明善暗恶"


def test_glossary_keeps_descriptive_term_unsplit():
    """含括号/冒号的描述句不是清单，不得拆成碎片（实测反例原样保留）。"""
    term = "灵气复苏等级体系（如：觉醒者、超凡者、半仙、真仙等对应低武/高武/修仙阶段）"
    assert split_terms(term) == [term]
    bible = _bible(glossary=[{"term": term, "note": ""}])
    body = _section_line(build_system_prompt(bible, [], 1, 3), "- 专有名词").split("：", 1)[1]
    assert body == term


def test_normalize_glossary_is_order_independent():
    """骨架条目与详细条目谁先来，note 都归到更详尽的那个。"""
    skeleton = {"term": "声望值", "note": ""}
    detailed = {"term": "声望值", "note": "系统核心货币"}
    assert normalize_glossary([skeleton, detailed])[0]["note"] == "系统核心货币"
    assert normalize_glossary([detailed, skeleton])[0]["note"] == "系统核心货币"


def test_dedup_keep_order():
    assert dedup_keep_order(["b", "a", "b", " ", "", "a"]) == ["b", "a"]


# ---------------------------------------------------------------- Forge 写入侧（根因）

def test_coerce_str_list_dedups():
    """禁令表根因侧：模型吐重复项时写入前就归并（实测 38 条含 2 组精确重复）。"""
    assert _coerce_str_list(["毫无疑问地", "毫无疑问的", "毫无疑问地", "毫无疑问的"]) == \
        ["毫无疑问地", "毫无疑问的"]


def test_coerce_str_list_dict_branch_still_works_and_dedups():
    """dict → "键：值" 的历史行为不能丢（2026-09-04 真机事故的防线）。"""
    assert _coerce_str_list({"社会结构": "以修士为核心", "科技": "以灵器为核心"}) == \
        ["社会结构：以修士为核心", "科技：以灵器为核心"]


def test_coerce_str_list_empty_returns_none():
    assert _coerce_str_list([]) is None
    assert _coerce_str_list("") is None
    assert _coerce_str_list(3.14) is None


# ---------------------------------------------------------------- P1-5 JSON 约束

class _RecordingProvider:
    def __init__(self, content: str = '{"retell": false}') -> None:
        self.content = content
        self.req = None

    def complete(self, req):
        self.req = req
        return LLMResult(ok=True, content=self.content)


def test_seam_retell_sets_json_response_format():
    prov = _RecordingProvider()
    out = _seam_retell(prov, "sys", "上文结尾", "新片段开头")
    assert prov.req.response_format == "json_object"
    assert out == ""


def test_seam_retell_still_parses_hit():
    """补 response_format 不改变语义：命中复述仍返回描述、供 reject note 重生成。"""
    prov = _RecordingProvider('{"retell": true, "what": "又一次被当众羞辱"}')
    assert _seam_retell(prov, "sys", "A", "B") == "又一次被当众羞辱"


def test_usage_default_cache_fields_are_none():
    """默认构造的 Usage 缓存字段为 None（fake/本地 provider 路径不会伪装成 0）。"""
    u = Usage()
    assert u.cache_hit_tokens is None and u.cache_miss_tokens is None
