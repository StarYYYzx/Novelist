"""蓝图列表字段防御归一回归（2026-09-04 云端 Qwen3.6-35B 真机实证）。

强模型把 civilizations/rules/factions/tone/forbidden_words 输出成 dict（分类键值）
或单串，schema 要求 string 数组——此前 bp.save 直接 SchemaError 崩掉整次构建。
"""

from __future__ import annotations

import pytest

from novelist.forge.nodes import _coerce_str_list, _merge_style, _merge_worldview
from novelist.forge.state import Blueprint


def _blank() -> Blueprint:
    bp = Blueprint.blank({"title": "测试书", "genre": "通用", "logline": "测试一句话",
                          "scale": {"volumes": 1, "chapters_per_volume": 5,
                                    "target_words_per_chapter": 2400}})
    return bp


def test_coerce_str_list_dict_to_kv_list():
    v = {"社会结构": "武者成为中层支柱", "科技": "灵能枪械"}
    assert _coerce_str_list(v) == ["社会结构：武者成为中层支柱", "科技：灵能枪械"]


def test_coerce_str_list_string_to_single():
    assert _coerce_str_list("低魔时代") == ["低魔时代"]
    assert _coerce_str_list("  ") is None


def test_coerce_str_list_passthrough_and_mixed():
    assert _coerce_str_list(["a", "b"]) == ["a", "b"]
    assert _coerce_str_list(["a", {"k": "v"}, 3]) == ["a", "k：v", "3"]
    assert _coerce_str_list([]) is None
    assert _coerce_str_list(123) is None


def test_merge_worldview_dict_civilizations_passes_schema(tmp_path):
    """真机复现：civilizations 为 dict 时合并后 bp.save 不再崩。"""
    bp = _blank()
    _merge_worldview(bp, {
        "name": "蓝星·灵气复苏",
        "civilizations": {"社会结构": "武者支柱", "科技": "灵能枪械"},
        "rules": ["灵气稀薄，高阶法术难以持久"],
        "factions": "官方特殊事务局",
    })
    bp.save(ws := _ws(tmp_path), "p1")  # save 内含 schema 校验，崩即失败
    wv = bp.get("worldview")
    assert wv["civilizations"] == ["社会结构：武者支柱", "科技：灵能枪械"]
    assert wv["factions"] == ["官方特殊事务局"]


def test_merge_style_dict_tone_passes_schema(tmp_path):
    bp = _blank()
    _merge_style(bp, {"tone": {"基调": "咸鱼吐槽", "底色": "降维打击"},
                      "forbidden_words": {"网络语": "yyds"}})
    bp.save(_ws(tmp_path), "p2")
    st = bp.get("style")
    assert st["tone"] == ["基调：咸鱼吐槽", "底色：降维打击"]
    assert st["forbidden_words"] == ["网络语：yyds"]


def _ws(root):
    from novelist.storage.workspace import Workspace

    w = Workspace(root=str(root))
    w.create_project("p1")
    w.create_project("p2")
    return w
