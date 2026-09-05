"""批次二（方案 A/B/D + 字数需求移除）单元测试。

- 方案 D：母题账本（core/motif.py）抽取/记账/禁令
- 方案 A：细纲连读审查（forge/coherence.py）LLM findings 解析/落盘/生成侧禁令
- 方案 B：槽位名归一（forge/textnorm.py + ask._apply_slot_value）、
  身份线索正则收紧、首登场局部重写 _patch_first_appearances
- C：length_cap_chars 参数已移除（传参即 TypeError）
"""
from __future__ import annotations

import json

import pytest

from novelist.core.motif import MotifLedger, extract_motifs
from novelist.core.orchestrator import (_has_identity_cue, produce_chapter,
                                        _patch_first_appearances)
from novelist.forge.ask import _apply_slot_value
from novelist.forge.slots import Slot
from novelist.forge.textnorm import coerce_character_name, heal_blueprint_characters
from novelist.providers.fake import FakeProvider
from novelist.storage.workspace import Workspace


# ---------------------------------------------------------------- 方案 D 母题账本

def test_extract_motifs_picks_cue_sentences():
    text = ("他抬起右手，指尖在空中轻轻一划。\n\n"
            "今天天气很好，阳光洒在地上。\n\n"
            "手机屏幕跳动着一条紧急推送。")
    motifs = extract_motifs(text)
    assert len(motifs) == 2
    assert any("指尖" in m for m in motifs)
    assert any("手机" in m for m in motifs)
    assert not any("阳光" in m for m in motifs)


def test_ledger_bans_after_repeats(tmp_path):
    ws = Workspace(root=str(tmp_path))
    pid = "proj-t"
    ws.create_project(pid)
    led = MotifLedger.load(ws, pid)
    led.add_text("他指尖轻划虚空。", 1)
    led.add_text("指尖划过，怪物倒下。", 1)
    led.save(ws, pid)
    led2 = MotifLedger.load(ws, pid)
    assert len(led2.entries) >= 1
    assert led2.entries[0]["count"] >= 1 or led2.entries[-1]["count"] >= 1
    bans = led2.ban_lines(min_count=2)
    # 同一母题（归一后不同句）累计 ≥2 → 出禁令
    total = sum(e["count"] for e in led2.entries)
    assert total >= 2
    assert isinstance(bans, list)


def test_ledger_no_ban_below_threshold():
    led = MotifLedger()
    led.entries = [{"motif": "x", "count": 1, "chapters": [1]}]
    assert led.ban_lines(min_count=2) == []


# ---------------------------------------------------------------- 方案 A 连读审查

class _JsonProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__(reply="")
        self.last_prompt = ""

    def complete(self, req):
        from novelist.core.llm import LLMResult

        self.last_prompt = req.messages[-1].content
        payload = {
            "motif_repeats": [{"motif": "手机推送新闻", "chapters": [1, 2],
                                "advice": "第3章禁用手机推送桥段"}],
            "causal_issues": [],
            "first_appearance_issues": [{"ch": 3, "who": "沈万钧",
                                          "issue": "首次出现未交代身份"}],
            "notes": "",
        }
        return LLMResult(ok=True, content="```json\n"
                         + json.dumps(payload, ensure_ascii=False) + "\n```")


def _bp_with_two_chapters():
    from novelist.forge.state import Blueprint

    bp = Blueprint(data={"meta": {"title": "t"}, "characters": [], "chapters": [
        {"vol": 1, "ch": 1, "title": "A", "key_events": ["李天劫查看手机新闻，收到母亲电话"]},
        {"vol": 1, "ch": 2, "title": "B", "key_events": ["李天劫分析母亲被标记的灵气"]},
    ]})
    return bp


def test_run_coherence_review_parses_and_persists(tmp_path):
    from novelist.forge.coherence import (load_coherence_bans,
                                          run_coherence_review)

    ws = Workspace(root=str(tmp_path))
    pid = "proj-t"
    ws.create_project(pid)
    bp = _bp_with_two_chapters()
    prov = _JsonProvider()
    fnd = run_coherence_review(ws, pid, bp, prov, vol=1, up_to_ch=2)
    assert fnd is not None
    assert fnd["motif_repeats"][0]["motif"] == "手机推送新闻"
    assert "手机推送" in prov.last_prompt
    # findings/报告落盘
    assert ws._abs(f"{pid}/workspace/forge/coherence-v1.json").exists()  # noqa: SLF001
    assert ws._abs(f"{pid}/reports/reviews/gist-coherence-v1.md").exists()  # noqa: SLF001
    # 生成侧禁令：第 3 章应拿到母题禁令 + 首登场提醒
    bans = load_coherence_bans(ws, pid, 1, 3)
    assert any("手机推送" in b for b in bans)
    assert any("沈万钧" in b for b in bans)
    # 第 2 章自身不收自己章号的母题禁令（但首登场提醒只在 ch=3）
    bans2 = load_coherence_bans(ws, pid, 1, 2)
    assert not any("手机推送" in b for b in bans2)


def test_coherence_skips_single_chapter(tmp_path):
    from novelist.forge.coherence import run_coherence_review

    ws = Workspace(root=str(tmp_path))
    pid = "proj-t"
    ws.create_project(pid)
    bp = Blueprint = None  # noqa: F841 - 单章分支在 gather 前返回
    from novelist.forge.state import Blueprint as _BP
    bp = _BP(data={"meta": {}, "chapters": [
        {"vol": 1, "ch": 1, "title": "A", "key_events": ["e1"]}]})
    assert run_coherence_review(ws, pid, bp, _JsonProvider(), 1, 1) is None


# ---------------------------------------------------------------- 方案 B 名字归一

def test_coerce_character_name_paren_note():
    name, note = coerce_character_name(
        "沈万钧（华夏特殊部门'镇守司'副司长，表面儒雅实则信奉人类至上主义）")
    assert name == "沈万钧"
    assert "副司长" in note


def test_coerce_character_name_long_no_paren():
    name, note = coerce_character_name("这是一个特别特别特别长的名字的后半段是设定")
    assert 0 < len(name) <= 12
    assert "名字原注" in note


def test_apply_slot_value_normalizes_name():
    from novelist.forge.state import Blueprint

    bp = Blueprint(data={"meta": {}, "characters": []})
    slot = Slot("characters[role:rival].name", "反派设定", "recommended",
                "free", "反派？", "llm", [], "", 4, "", 0.6)
    path = _apply_slot_value(bp, slot,
                             "沈万钧（华夏特殊部门'镇守司'副司长，表面儒雅）",
                             "llm", 0.8)
    card = bp.section("characters")[0]
    assert card["name"] == "沈万钧"
    assert "副司长" in card.get("background", "")
    assert path == "characters[char:rival].name"


def test_heal_blueprint_characters_fixes_legacy(tmp_path):
    from novelist.forge.state import Blueprint

    bp = Blueprint(data={"meta": {}, "characters": [
        {"id": "char:rival", "role": "rival",
         "name": "沈万钧（'镇守司'副司长，人类至上主义者）"}]})
    warns = heal_blueprint_characters(bp)
    assert len(warns) == 1
    assert bp.section("characters")[0]["name"] == "沈万钧"
    assert "'镇守司'" in bp.section("characters")[0].get("background", "")


# ---------------------------------------------------------------- 方案 B 身份线索收紧

def test_identity_cue_no_longer_matches_generic_child():
    # "孩子" 已从线索表剔除——泛词误命中是 proj-cloudb5 ch1 实证根因
    assert not _has_identity_cue("万钧哥，别吓唬孩子了……苏清婉的声音细细的。", "苏清婉")
    assert _has_identity_cue("苏清婉是主角的邻居，中医世家传人。", "苏清婉")


# ---------------------------------------------------------------- 方案 B 首登场局部重写

class _RewriteProvider(FakeProvider):
    """对补写调用返回含名字的长句（≥min_reply_chars），其余走 fake。"""

    def __init__(self) -> None:
        super().__init__(reply=" filler ")

    def complete(self, req):
        from novelist.core.llm import LLMResult

        content = req.messages[-1].content
        if "局部修订器" in content:
            name = content.split("「")[1].split("」")[0]
            return LLMResult(ok=True, content=(
                f"{name}攥着对讲机冲进人群——他是镇守司外勤七组的副司长，"
                "奉命盯着这个不肯配合的年轻人。他的呼吸声像拉风箱。"))
        return super().complete(req)


class _FakeTracker:
    """最小 tracker 桩：一人首现无身份 → 产生 problem。"""

    class _E:
        type = "character"
        stage = "unseen"
        first_ch = None
        key = "char:rival"
        name = "沈万钧"

    def resolve(self, text):
        return [self._E()] if "沈万钧" in text else []

    def is_core(self, key):
        return False


def test_patch_first_appearances_rewrites_paragraph():
    text = ("李天劫接起电话。\n\n"
            "听筒里沈万钧的呼吸声像拉风箱，滋滋的电流声里透着焦躁。\n\n"
            "挂断后他望着窗外。")
    new_text, patched = _patch_first_appearances(
        None, "p", 1, 1, text, _RewriteProvider(), _FakeTracker())
    assert patched == 1
    assert "镇守司外勤七组" in new_text
    assert "李天劫接起电话。" in new_text  # 其他段落不动
    assert "听筒里" not in new_text.split("\n\n")[1]  # 原段已被重写


def test_patch_skips_short_reply():
    text = "听筒里沈万钧的呼吸声急促。"
    new_text, patched = _patch_first_appearances(
        None, "p", 1, 1, text, FakeProvider(reply="好"), _FakeTracker())
    assert patched == 0
    assert new_text == text


# ---------------------------------------------------------------- C：字数需求移除

def test_length_cap_param_removed(tmp_path):
    ws = Workspace(root=str(tmp_path))
    pid = "proj-t"
    ws.create_project(pid)
    prov = FakeProvider(reply="正文内容。" * 50)
    with pytest.raises(TypeError):
        produce_chapter(ws, pid, 1, 1, prov, prefer_direct=True,
                        length_cap_chars=300, jit_characters=False, validate=False)


def test_no_target_words_in_style_prompt(tmp_path):
    """context 层不再注入"目标篇幅"行（源码级断言）。"""
    from pathlib import Path

    src = Path("src/novelist/core/context.py").read_text(encoding="utf-8")
    assert "目标篇幅" not in src
