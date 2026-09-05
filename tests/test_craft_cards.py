"""题材工艺卡（src/novelist/craft）回归。

动机（2026-09-05 lingyu5 真机事故）：设定敲定阶段只拍板了"写什么"，
"怎么呈现"全交给 style 节点的模型 → 模型把"系统提示音/叮/机械音"当爽文
俗套写进 forbidden_words（Genre Pack 自带禁用词并无这三项），结果一本系统流
小说 7654 字里系统零次发声，题材根基缺位。工艺卡把呈现规范固化成硬约束，
在 style 节点与正文生成两处注入。
"""

from __future__ import annotations

from novelist.core.context import build_system_prompt
from novelist.craft import loader


# ---- loader ----
def test_list_cards_parses_frontmatter():
    cards = loader.list_cards()
    ids = {c.id for c in cards}
    assert {"system-flow", "invincible-flow", "foreshadowing", "chapter-rhythm"} <= ids
    for c in cards:
        assert c.name and c.summary
        assert c.inject, f"{c.id} 缺少 INJECT 注入段"
        assert "INJECT" not in c.inject, "注入段不得残留标记"
    sf = next(c for c in cards if c.id == "system-flow")
    assert sf.scope == ["style", "chapter"]
    # knobs 解析：行尾注释必须剥离
    assert sf.knobs["notify_format"] == "bracket"
    assert sf.knobs["persona"] == "neutral_mechanical"
    assert "#" not in str(sf.knobs)


def test_inject_block_concat_and_skips_unknown():
    blk = loader.inject_block(["system-flow", "chapter-rhythm"])
    assert "【系统呈现规范" in blk and "【单章节奏规范" in blk
    assert "INJECT" not in blk
    # 未知 id 静默跳过，不抛异常
    assert loader.inject_block(["no-such-card"]) == ""
    assert loader.inject_block(None) == ""
    assert loader.inject_block(["system-flow", "no-such-card"]).count("【系统呈现规范") == 1


# ---- style 节点：不得把工艺卡规定的呈现标识列为禁用词 ----
def test_style_node_injects_craft_guard():
    from novelist.forge.nodes import _craft_block

    assert _craft_block({}) == ""
    assert _craft_block({"craft_cards": []}) == ""
    out = _craft_block({"craft_cards": ["system-flow"]})
    assert "【系统呈现规范" in out
    # 关键：反俗套的禁用词表不得误伤题材核心标识
    assert "不得写入 forbidden_words" in out
    assert "【】" in out


# ---- 正文生成 prompt 注入 ----
def _bible(craft: list[str] | None, words: int | None = None) -> dict:
    st: dict = {"craft_cards": craft or []}
    if words:
        st["target_words_per_chapter"] = words
    return {"worldview": {"name": "九州", "power_system": {"levels": ["炼气", "筑基"]}},
            "characters": [], "style": st}


def test_context_injects_craft_block():
    out = build_system_prompt(_bible(["system-flow"]), [], 1, 2)
    assert "【系统呈现规范（本书为系统流，硬约束）】" in out
    assert "【】只能用于包裹系统的发言" in out
    assert "权限不足" in out


def test_context_no_craft_block_when_unset():
    out = build_system_prompt(_bible([]), [], 1, 2)
    assert "系统呈现规范" not in out


def test_no_word_target_injected_even_with_chapter_rhythm():
    """篇幅纪律只说"补什么"，不注入字数目标——2026-09-05 用户决定：
    注入字数会诱发凑字（全局守卫见 test_batch2_adb.py）。
    """
    out = build_system_prompt(_bible(["chapter-rhythm"], words=2400), [], 1, 2)
    assert "【单章节奏规范" in out
    assert "目标篇幅" not in out
    assert "2400" not in out
    assert "不靠主角内心独白" in out


def test_apply_slot_value_craft_cards_is_list():
    """回归：工艺卡是多选列表。蓝图未预置空列表时（非 seed 路径），
    通用分支会把值写成裸字符串 → schema 崩（2026-09-05 真机）。
    """
    from novelist.forge.ask import _apply_slot_value
    from novelist.forge.slots import Slot
    from novelist.forge.state import Blueprint

    def _bp():
        return Blueprint.blank(meta={"title": "t", "genre": "g", "logline": "l",
                                     "scale": {"volumes": 3, "chapters_per_volume": 8,
                                               "target_words_per_chapter": 2400}})

    slot = Slot("style.craft_cards", "题材工艺卡", "recommended", "free",
                "", "enum", [], "", 3, "", 0.6)
    bp = _bp()
    _apply_slot_value(bp, slot, "system-flow、invincible-flow", "user", 1.0)
    assert bp.get("style.craft_cards") == ["system-flow", "invincible-flow"]

    bp2 = _bp()  # 未预置空列表的蓝图，单选也必须落成列表
    _apply_slot_value(bp2, slot, "chapter-rhythm", "user", 1.0)
    assert bp2.get("style.craft_cards") == ["chapter-rhythm"]
    bp2.validate()


# ---- CLI ----
def test_cli_forge_craft_lists_cards(tmp_path):
    from click.testing import CliRunner

    from novelist.cli import cli

    r = CliRunner().invoke(cli, ["forge", "craft", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "system-flow" in r.output and "系统流" in r.output
    assert "chapter-rhythm" in r.output


# ---- 设定阶段槽位 ----
def test_slot_table_exposes_craft_cards():
    from novelist.forge.slots import default_slots

    slot = next((s for s in default_slots() if s.key == "style.craft_cards"), None)
    assert slot is not None, "设定敲定阶段必须能勾选工艺卡"
    assert slot.enum and "system-flow" in slot.enum
    assert slot.group == 3
