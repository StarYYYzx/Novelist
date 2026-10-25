"""工艺卡实例化测试（DL-015⑦，2026-10-24）：卡=构建期参考资料 → 本书 craft_rules。

单测绝不真调 LLM（docs/09 §2.1）——FakeProvider / ScriptedProvider。
"""

from __future__ import annotations

import json

from novelist.craft.instantiate import eval_craft_checks, instantiate_craft_rules
from novelist.craft.loader import load_card
from novelist.providers.fake import FakeProvider, ScriptedProvider
from novelist.storage.workspace import Workspace

CH = ("林澈盘膝而坐，丹田内灵气翻涌。他握紧了那块残缺玉佩。"
      "「明日宗门大比，我定要让所有人闭嘴。」他低声说。")


def _ws(tmp_path, pid="proj-craft") -> tuple[Workspace, str]:
    ws = Workspace(root=str(tmp_path))
    ws.create_project(pid)
    return ws, pid


# ---------- 卡格式（loader） ----------

def test_system_flow_card_has_phase_and_checks():
    c = load_card("system-flow")
    assert c.phase == "compile"
    kinds = {x.get("id") for x in c.checks}
    assert "sys_appear_min" in kinds and "sys_appear_max" in kinds
    assert c.inject  # INJECT 段保留（实例化模板）


def test_romance_card_merged_and_mode_sections():
    """三卡合一：harem/no-heroine/single-heroine 已删，romance 按 mode 分节。"""
    c = load_card("romance")
    assert c is not None and c.phase == "compile"
    for mode in ("none", "single", "harem", "de-emphasized"):
        assert f"## mode：{mode}" in c.inject
    assert load_card("harem") is None
    assert load_card("no-heroine") is None
    assert load_card("single-heroine") is None


# ---------- 实例化 ----------

def test_instantiate_romance_mode_from_slot():
    """联动拍板：meta.romance=单女主 → romance 卡以 single 小节实例化。"""
    rules = instantiate_craft_rules(
        {"craft_cards": ["romance"]}, {"romance": "单女主"})
    assert len(rules) == 1
    r = rules[0]
    assert r["card"] == "romance" and r["mode"] == "single" and r["phase"] == "compile"
    assert "单女主：感情线只围绕女主展开" in r["rule"]
    assert "## mode：none" not in r["rule"] and "## mode：harem" not in r["rule"]

    rules2 = instantiate_craft_rules(
        {"craft_cards": ["romance"]}, {"romance": "无CP"})
    assert "禁止任何" in rules2[0]["rule"] and rules2[0]["mode"] == "none"


def test_instantiate_carries_checks_template():
    rules = instantiate_craft_rules({"craft_cards": ["system-flow"]}, {})
    r = rules[0]
    assert r["checks"] and r["checks"][0]["id"] == "sys_appear_min"
    assert "【】" in r["rule"]  # INJECT 模板随行


# ---------- 机检谓词 ----------

def test_eval_craft_checks_min_max_and_not_contains():
    rules = [{"card": "system-flow", "phase": "compile", "rule": "",
              "checks": [
                  {"id": "sys_appear_min", "kind": "min_count", "pattern": "【", "min": 1},
                  {"id": "sys_appear_max", "kind": "max_count", "pattern": "【", "max": 8},
                  {"id": "no_meta", "kind": "not_contains", "pattern": "突然之间",
                   "note": "元叙事套话禁用"},
              ]}]
    v = eval_craft_checks(rules, CH)
    ids = {x["check_id"] for x in v}
    assert "sys_appear_min" in ids            # 全章无【 → 违例
    assert "no_meta" not in ids               # 未出现 → 不违例
    # 出现 9 处【 → 超上限违例
    spam = CH + "【任务】" * 9
    v2 = eval_craft_checks(rules, spam)
    assert "sys_appear_max" in {x["check_id"] for x in v2}
    # 坏 pattern / 未知 kind 静默跳过（卡是参考资料不炸编译）
    bad = [{"card": "x", "phase": "compile", "rule": "",
            "checks": [{"id": "b1", "kind": "min_count", "pattern": "([", "min": 1},
                       {"id": "b2", "kind": "wat", "pattern": "x", "min": 1}]}]
    assert eval_craft_checks(bad, CH) == []
    # polish 相的条目生成期不检
    assert eval_craft_checks([{"card": "p", "phase": "polish", "rule": "",
                               "checks": [{"id": "p1", "kind": "min_count",
                                           "pattern": "【", "min": 1}]}], CH) == []


# ---------- seed 集成：默认卡 + 实例化落盘 ----------

def test_seed_smoke_default_cards_and_craft_rules(tmp_path):
    """类型包默认卡自动带上（未显式勾选），且确定性实例化进 style.craft_rules。"""
    from novelist.forge.seed import run_seed

    ws, pid = _ws(tmp_path)
    reply = json.dumps({
        "genre": "修仙", "template_suggestion": "", "logline": "少年逆袭",
        "protagonist_hint": {"name": "林澈", "gender": "male", "cheat": "系统"},
        "conflict": "", "tone_hint": "",
        "scale_hint": {"volumes": 2, "chapters_per_volume": 8},
        "time_origin": "", "anchors": [], "unknowns": [],
    }, ensure_ascii=False)
    res = run_seed(ws, pid, "修仙少年", provider=FakeProvider(reply=reply),
                   mode="auto", smoke=True)
    assert res.ok
    bp = json.loads(ws._abs(f"{pid}/workspace/forge/blueprint.json")  # noqa: SLF001
                    .read_text(encoding="utf-8"))
    cards = bp["style"]["craft_cards"]
    rules = bp["style"]["craft_rules"]
    # 通用包默认卡 chapter-rhythm（DL-015：防漏勾）
    assert "chapter-rhythm" in cards
    assert rules and all(r.get("card") in cards for r in rules)


# ---------- 编译闸门集成：机检违例 → hard → 重写修复 ----------

def test_compile_pass_craft_check_gate(tmp_path):
    from novelist.consistency.tribunal import compile_pass

    ws, pid = _ws(tmp_path)
    # bible/style.json 带系统流机检规则；正文无【 → 第一轮必违例
    ws.write_json(ws._abs(f"{pid}/bible/style.json"), {  # noqa: SLF001
        "craft_rules": [{"card": "system-flow", "phase": "compile",
                         "rule": "【】专用",
                         "checks": [{"id": "sys_appear_min", "kind": "min_count",
                                     "pattern": "【", "min": 1,
                                     "note": "系统每章至少露面一次"}]}]})
    clean = {"final": json.dumps({"issues": []}, ensure_ascii=False)}
    fixed = {"final": CH + "【任务：击败同门。】" + "修订扩充。" * 80}  # 重写稿带【且够长
    script = [clean, clean, clean, fixed, clean, clean, clean]
    text, report = compile_pass(ws, pid, 1, 1, CH, ScriptedProvider(script), rewrite_max=2)
    assert report["blocked"] is False and report["rounds"] == 2
    assert "【任务" in text                    # 修订稿被采纳
    kinds = {h["type"] for h in report["hard"]}
    # hard 里含 rules 视角的机检违例（第一轮记录后第二轮清零——报告是末轮）
    assert "工艺规则违反（sys_appear_min）" in kinds or not report["hard"]


# ---------- 生成注入切换：craft_rules 优先 ----------

def test_system_prompt_prefers_instantiated_rules(tmp_path):
    from novelist.core.context import build_system_prompt

    ws, pid = _ws(tmp_path)
    bible = {
        "style": {"craft_cards": ["system-flow"],
                  "craft_rules": [{"card": "system-flow", "phase": "compile",
                                   "rule": "【本书专用规则：系统发言用【】包裹】"}]},
    }
    out = build_system_prompt(bible, [], 1, 1)
    assert "本书工艺规则 · system-flow" in out
    assert "【本书专用规则" in out
    # 存量回退：无 craft_rules → 注入卡的 INJECT 原文
    bible_legacy = {"style": {"craft_cards": ["system-flow"]}}
    out2 = build_system_prompt(bible_legacy, [], 1, 1)
    assert "只能用于包裹系统发言" in out2 or "系统呈现规范" in out2


def test_eval_craft_checks_requires_protagonist_skips_when_absent():
    """多视角修正（F13）：requires=protagonist 的谓词在主角不在场的章跳过——
    整章他人视角时强迫系统【】露面 = 违反同卡"系统只对主角可见"。"""
    from novelist.craft.instantiate import set_presence_context

    set_presence_context([{"id": "char:lin", "role": "protagonist"},
                          {"id": "char:x"}])
    rules = [{"card": "system-flow", "phase": "compile", "rule": "",
              "checks": [{"id": "sys_appear_min", "kind": "min_count",
                          "pattern": "【", "min": 1, "requires": "protagonist"}]}]
    # 主角在场：无【 仍违例
    assert eval_craft_checks(rules, CH, ["char:lin"]) != []
    # 主角不在场：同文本跳过（阳性缺席判定）
    assert eval_craft_checks(rules, CH, ["char:x"]) == []
    # 人物卡未注入：保守执行（无法证明缺席 → 检查照跑，漏检有三视角兜底）
    set_presence_context(None)
    assert eval_craft_checks(rules, CH) != []


def test_set_presence_context_protagonist_resolution():
    from novelist.craft.instantiate import eval_craft_checks, set_presence_context

    set_presence_context([{"id": "char:a", "role": "protagonist"},
                          {"id": "char:b"}])
    rules = [{"card": "c", "phase": "compile", "rule": "",
              "checks": [{"id": "x", "kind": "min_count", "pattern": "【", "min": 1,
                          "requires": "protagonist"}]}]
    assert eval_craft_checks(rules, CH, ["char:a"]) != []   # 主角在场 → 检
    assert eval_craft_checks(rules, CH, ["char:b"]) == []   # 主角不在 → 跳
    set_presence_context(None)  # 清理，不污染其他测试
