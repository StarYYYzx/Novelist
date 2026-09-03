"""D13：brief 硬约束锚点——seed 抽取（LLM + 正则兜底）与 forge prompt 注入。

背景：一句话 brief 说"灵气复苏仅过去了一年"，seed 提炼 time_origin 时被静默改写为
"第三年"，下游 book/volume/chapter 全不知情。修法：anchors 原话摘抄入
meta.anchors（user provenance），所有 forge prompt 以「不得改动」注入。
"""

from __future__ import annotations

from novelist.forge.seed import _init_blueprint, _parse_seed_spec
from novelist.forge.nodes import NodeContext, _book_prompt, _chapter_prompt, _volume_prompt
from novelist.providers.fake import FakeProvider

from test_m13_forge_f1 import SEED_REPLY

BRIEF = ("大学生李天劫23岁，渡劫圆满飞升出错回到蓝星，灵气复苏仅过去了一年，"
         "人们修为普遍不高，渡劫圆满的他天下无敌。")


def _mk_spec(reply_overrides: dict | None = None):
    data = __import__("json").loads(SEED_REPLY)
    if reply_overrides:
        data.update(reply_overrides)
    import json

    return _parse_seed_spec(json.dumps(data, ensure_ascii=False))


def _mk_bp(ws, pid, spec, brief):
    from novelist.forge.genres import load_pack_for

    return _init_blueprint(ws, pid, brief, spec, load_pack_for("修仙男频"),
                           "修仙男频", 1, 2, 1000)


def _mk_ctx(ws, pid, bp, kind="book", vol=0, ch=0):
    from novelist.storage.workspace import Workspace  # noqa: F401

    return NodeContext(ws=ws, project_id=pid, bp=bp, provider=FakeProvider(reply=""),
                       pack={}, vol=vol, ch=ch)


# ---- 抽取 ----
def test_regex_fallback_catches_chinese_numerals(ws_factory):
    """LLM 没给 anchors 时，正则兜底必须抓到中文数字时间跨度。"""
    ws, pid = ws_factory("proj-d13a")
    bp = _mk_bp(ws, pid, _mk_spec({"anchors": []}), BRIEF)
    anchors = bp.get("meta.anchors") or []
    assert any("一年" in a for a in anchors), anchors
    assert any("23岁" in a or "23 岁" in a for a in anchors), anchors
    assert bp.get_provenance("meta.anchors")["src"] == "user"


def test_llm_anchors_deduped_with_regex(ws_factory):
    """LLM 摘抄与正则结果查重合并，不重复。"""
    ws, pid = ws_factory("proj-d13b")
    spec = _mk_spec({"anchors": ["灵气复苏仅过去了一年"]})
    bp = _mk_bp(ws, pid, spec, BRIEF)
    anchors = bp.get("meta.anchors") or []
    assert anchors.count("灵气复苏仅过去了一年") == 1
    assert len(anchors) >= 2  # 23岁 短句也该在


def test_no_false_positive_on_non_quantity_numerals(ws_factory):
    """非数量词的数字短语不误捕。"""
    ws, pid = ws_factory("proj-d13c")
    bp = _mk_bp(ws, pid, _mk_spec({"anchors": []}), "写一个一句话创意的测试")
    assert bp.get("meta.anchors") in (None, [])


# ---- 注入 ----
def test_book_prompt_carries_anchors(ws_factory):
    ws, pid = ws_factory("proj-d13d")
    bp = _mk_bp(ws, pid, _mk_spec({"anchors": ["灵气复苏仅过去了一年"]}), BRIEF)
    _, user = _book_prompt(_mk_ctx(ws, pid, bp))
    assert "灵气复苏仅过去了一年" in user
    assert "一字不得改写" in user


def test_chapter_prompt_carries_anchors(ws_factory):
    ws, pid = ws_factory("proj-d13e")
    bp = _mk_bp(ws, pid, _mk_spec({"anchors": ["灵气复苏仅过去了一年"]}), BRIEF)
    bp.data["characters"] = [{"id": "char:a", "name": "甲", "gender": "male"}]
    bp.data["volumes"] = [{"vol": 1, "title": "v", "summary": "s"}]
    _, user = _chapter_prompt(_mk_ctx(ws, pid, bp, vol=1, ch=1))
    assert "灵气复苏仅过去了一年" in user


def test_volume_prompt_carries_anchors(ws_factory):
    ws, pid = ws_factory("proj-d13f")
    bp = _mk_bp(ws, pid, _mk_spec({"anchors": ["灵气复苏仅过去了一年"]}), BRIEF)
    bp.data["volumes"] = [{"vol": 1, "title": "v", "summary": "s"}]
    _, user = _volume_prompt(_mk_ctx(ws, pid, bp, vol=1))
    assert "灵气复苏仅过去了一年" in user


def test_no_anchors_no_noise(ws_factory):
    ws, pid = ws_factory("proj-d13g")
    bp = _mk_bp(ws, pid, _mk_spec({"anchors": []}), "一句话创意（无数字）")
    _, user = _book_prompt(_mk_ctx(ws, pid, bp))
    assert "硬性锚点" not in user
