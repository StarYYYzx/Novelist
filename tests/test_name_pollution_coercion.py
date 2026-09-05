"""方案7（质量加固 2026-09-05）：角色卡 schema 防污染 + 实体别名归一。

真机实证（proj-cloud5）：name 塞整段人设（40 字）→ 广播误杀 + 实体双记；
两卡同采"青梅竹马"模板 → 卡间关系冲突。
"""

from __future__ import annotations

import json

from novelist.core.director import load_characters
from novelist.forge import Blueprint
from novelist.forge.nodes import (_coerce_character_card, _character_conflict_warnings,
                                  _merge_generic_cards)


def _bp():
    return Blueprint.blank({"title": "测试书", "genre": "修仙", "logline": "一句话",
                            "scale": {"volumes": 1, "chapters_per_volume": 5,
                                      "target_words_per_chapter": 2400}})


# ---- name 归一 ----

def test_coerce_bracket_annotation_moves_to_background():
    card = {"id": "char:love_interest", "name": "苏晚晴（青梅竹马，前世主角最愧疚之人）",
            "background": "温柔隐忍"}
    card, warns = _coerce_character_card(card)
    assert card["name"] == "苏晚晴"
    assert "青梅竹马" in card["background"] and "温柔隐忍" in card["background"]
    assert warns == []


def test_coerce_overlong_name_cut_with_note():
    raw = "李天劫是青云宗最低调也最神秘的天才大学生"
    card, warns = _coerce_character_card({"id": "char:p", "name": raw})
    assert card["name"] == "李天劫"
    assert "名字原注" in card["background"] and raw in card["background"]
    assert warns and "超长" in warns[0]


def test_coerce_clean_name_untouched():
    card = {"id": "char:p", "name": "李天劫", "background": "穿越者"}
    out, warns = _coerce_character_card(dict(card))
    assert out["name"] == "李天劫" and out["background"] == "穿越者" and not warns


def test_coerce_missing_name_safe():
    card, warns = _coerce_character_card({"id": "char:x"})
    assert card.get("name") is None and not warns


# ---- 泛型卡合并 ----

def test_merge_generic_cards_by_name():
    bp = _bp()
    bp.upsert("characters", {"id": "char:protagonist", "name": "李天劫",
                             "role": "protagonist", "background": "前世渡劫圆满",
                             "core_traits": ["低调"]})
    bp.upsert("characters", {"id": "char:li_tianjie", "name": "李天劫",
                             "role": "protagonist"})
    warns = _merge_generic_cards(bp)
    ids = [c["id"] for c in bp.section("characters")]
    assert "char:protagonist" not in ids and "char:li_tianjie" in ids
    twin = bp.find_by_id("characters", "char:li_tianjie")
    assert twin["background"] == "前世渡劫圆满" and twin["core_traits"] == ["低调"]
    assert any("已合并" in w for w in warns)


def test_merge_generic_cards_no_twin_keeps():
    bp = _bp()
    bp.upsert("characters", {"id": "char:love_interest", "name": "苏晚晴",
                             "role": "love_interest", "background": "青梅竹马"})
    assert _merge_generic_cards(bp) == []
    assert bp.find_by_id("characters", "char:love_interest") is not None


# ---- 卡间冲突 ----

def test_character_conflict_warning_on_template_collision():
    bp = _bp()
    bp.upsert("characters", {"id": "char:a", "name": "苏晚晴", "role": "love_interest",
                             "background": "李天劫的青梅竹马"})
    bp.upsert("characters", {"id": "char:b", "name": "林婉清", "role": "minor",
                             "core_traits": ["青梅竹马"]})
    warns = _character_conflict_warnings(bp)
    assert len(warns) == 1 and "青梅竹马" in warns[0] and "苏晚晴" in warns[0]


def test_character_conflict_no_warning_single_use():
    bp = _bp()
    bp.upsert("characters", {"id": "char:a", "name": "苏晚晴", "role": "love_interest",
                             "background": "李天劫的青梅竹马"})
    assert _character_conflict_warnings(bp) == []


# ---- 运行期视图兜底（director.load_characters） ----

def test_load_characters_sanitizes_polluted_name(ws_factory):
    ws, pid = ws_factory("proj-pollute")
    ws._abs(f"{pid}/bible").mkdir(parents=True, exist_ok=True)  # noqa: SLF001
    rows = [{"id": "char:love_interest", "name": "苏晚晴（青梅竹马，前世主角最愧疚之人）"}]
    (ws._abs(f"{pid}/bible/characters.json")  # noqa: SLF001
     ).write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    chars = load_characters(ws, pid)
    assert chars and chars[0]["name"] == "苏晚晴"
    assert "青梅竹马" in chars[0].get("background", "")
