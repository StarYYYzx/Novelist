"""线索（Line）子系统回归（ADR-025，docs/线索子系统设计与规划-2026-09-06.md）。

覆盖批 1 全链路：
- 数据层：core/lines.py 账本读写/校验/冷却/视图/动作落账/提名转正/Chronicler 行解析；
- 规划侧：book 骨架登记（主线唯一硬校验）、卷纲五元组、章纲 lines_present（细纲 md 携带）；
- 生成侧：prompt_budget 钉死层、事件线卡注入、章字数下限注入（85%）；
- 回写侧：Chronicler 线索行解析与账本回写（计划内闭合/计划外 candidate/pending 提名）。

失败降级纪律（ADR-021）：账本缺失 = 空账本，所有注入点静默跳过，不改变旧版行为。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novelist.core import lines as L
from novelist.storage.workspace import Workspace


def _ws(tmp_path) -> Workspace:
    ws = Workspace(root=str(tmp_path))
    ws.create_project("proj-ln")
    return ws


def _ledger(main_vol: int = 2) -> list[dict]:
    return [
        L.new_line("ln:main", "主角向青云宗复仇", kind="main",
                   target={"vol": main_vol, "note": "弑仇"}),
        L.new_line("ln:sect", "青云宗暗查青山镇", kind="subplot",
                   members=["char:chen"], target={"vol": 1, "note": "执法堂立项"}),
        L.new_line("ln:jade", "古玉低语", kind="hidden"),
    ]


# ---------------------------------------------------------------- 数据层

def test_load_missing_ledger_returns_empty(tmp_path):
    ws = _ws(tmp_path)
    assert L.load_lines(ws, "proj-ln") == []
    # 全部 API 在空账本上安全
    assert L.validate_lines([]) == []
    blk, warns = L.chapter_view([], 1, 1, 20)
    assert blk == "" and warns == []


def test_validate_main_unique_and_target(tmp_path):
    rows = [L.new_line("ln:a", "x", kind="main", target={"vol": 1, "note": "t"}),
            L.new_line("ln:b", "y", kind="main", target={"vol": 1, "note": "t"})]
    errs = L.validate_lines(rows)
    assert any("主线不唯一" in e for e in errs), errs
    rows2 = [L.new_line("ln:a", "x", kind="main")]  # main 缺 target
    assert any("target" in e for e in L.validate_lines(rows2))
    # closed 一致性（死线复活形态）
    dead = L.new_line("ln:c", "z")
    dead["status"], dead["closed"] = "active", {"vol": 1, "ch": 1, "reason": "r"}
    assert any("死线复活" in e for e in L.validate_lines([dead]))


def test_cooldown_warnings_tiered(tmp_path):
    rows = _ledger()
    for ln in rows[:2]:
        ln["status"], ln["opened"] = "active", {"vol": 1, "ch": 2}
        ln["last_seen"] = {"vol": 1, "ch": 2}
    # K=20：1-2 → 1-9 距 7 章
    warns = L.cooldown_warnings(rows, 1, 9, 20)
    assert any("ln:main" in x for x in warns)       # main 冷却 3
    assert any("ln:sect" in x for x in warns)       # subplot 冷却 5
    assert not any("ln:jade" in x for x in warns)   # hidden 跨卷合法，不查
    assert L.cooldown_warnings(rows, 1, 4, 20) == []  # 距 2 章，无告警


def test_line_card_format(tmp_path):
    ln = _ledger()[1]
    ln["status"], ln["opened"] = "active", {"vol": 1, "ch": 2}
    ln["last_seen"] = {"vol": 1, "ch": 3}
    ln["progress"] = ["1-2 宗门收到密报", "1-3 陈锋探院无实据"]
    card = L.line_card(ln, 1, 5, 20, note="推进→执法堂立项")
    assert card.startswith("ln:sect 支线·active")
    assert "上次1-3(距2章)" in card
    assert "1-2 宗门收到密报→1-3 陈锋探院无实据" in card
    assert "本章:推进→执法堂立项" in card


def test_chapter_view_pins_cooldown_and_closed_ban(tmp_path):
    rows = _ledger()
    rows[1]["status"], rows[1]["opened"] = "active", {"vol": 1, "ch": 2}
    rows[1]["last_seen"] = {"vol": 1, "ch": 2}
    rows[2]["status"], rows[2]["closed"] = "closed", {"vol": 1, "ch": 1, "reason": "揭穿"}
    blk, warns = L.chapter_view(rows, 1, 8, 20)
    assert "ln:sect" in blk
    assert "已闭合线索·禁复活" in blk and "ln:jade" in blk.split("禁复活")[1]
    assert any("冷却告警" in w and "ln:sect" in w for w in warns)
    # 主线恒在：main 卡先于支线出现（main 无冷却时按 kind 排序）
    rows2 = _ledger()
    for ln in rows2[:2]:
        ln["status"], ln["opened"] = "active", {"vol": 1, "ch": 2}
        ln["last_seen"] = {"vol": 1, "ch": 8}
    blk2, _ = L.chapter_view(rows2, 1, 9, 20)
    assert blk2.index("ln:main") < blk2.index("ln:sect")


# ---------------------------------------------------------------- 动作落账

def test_apply_chapter_actions_open_advance_suspend_flicker_close(tmp_path):
    ws = _ws(tmp_path)
    rows = _ledger()
    warns = L.apply_chapter_actions(ws, "proj-ln", rows, 1, 2, [
        {"id": "ln:sect", "action": "open", "note": "密报送到宗门"},
        {"id": "ln:main", "action": "open", "note": "x"},   # 已 active? 不——main 未开，
    ])
    assert rows[1]["status"] == "active" and rows[1]["opened"] == {"vol": 1, "ch": 2}
    # main 骨架行 open → active
    assert rows[0]["status"] == "active"
    # 落盘了
    assert L.load_lines(ws, "proj-ln")[1]["status"] == "active"
    # advance / flicker / suspend
    L.apply_chapter_actions(ws, "proj-ln", rows, 1, 3,
                            [{"id": "ln:sect", "action": "advance", "note": "探院无实据"}])
    assert rows[1]["last_seen"] == {"vol": 1, "ch": 3}
    L.apply_chapter_actions(ws, "proj-ln", rows, 1, 4,
                            [{"id": "ln:main", "action": "suspend", "note": "支线章"}])
    assert rows[0]["status"] == "suspended" and rows[0]["resume_hint"] == "支线章"
    L.apply_chapter_actions(ws, "proj-ln", rows, 1, 5,
                            [{"id": "ln:main", "action": "flicker", "note": "末尾露苗头"}])
    assert rows[0]["status"] == "suspended"          # flicker 不改状态，只留痕
    assert any("[露头]" in p for p in rows[0]["progress"])
    # advance 对 suspended = 显式恢复
    L.apply_chapter_actions(ws, "proj-ln", rows, 1, 6,
                            [{"id": "ln:main", "action": "advance", "note": "回归"}])
    assert rows[0]["status"] == "active"
    # close（章纲计划闭合）
    L.apply_chapter_actions(ws, "proj-ln", rows, 1, 7,
                            [{"id": "ln:sect", "action": "close", "note": "立项"}])
    assert rows[1]["status"] == "closed" and rows[1]["closed"]["ch"] == 7
    # 死线复活拦截
    warns2 = L.apply_chapter_actions(ws, "proj-ln", rows, 1, 8,
                                     [{"id": "ln:sect", "action": "advance", "note": "z"}])
    assert any("死线复活" in w for w in warns2)


def test_apply_chapter_actions_tail_bans_open(tmp_path):
    rows = _ledger()
    warns = L.apply_chapter_actions(ws=None or _ws(tmp_path), project_id="proj-ln",
                                    lines=rows, vol=1, ch=20,
                                    actions=[{"id": "ln:sect", "action": "open", "note": "n"}],
                                    tail_phase=True)
    assert rows[1]["status"] == "dormant"
    assert any("收尾期禁开线" in w for w in warns)


def test_progress_append_only_dedup(tmp_path):
    ws = _ws(tmp_path)
    rows = _ledger()
    rows[1]["status"], rows[1]["opened"] = "active", {"vol": 1, "ch": 2}
    for _ in range(2):  # 同章同文两次回写只记一次
        L.apply_chapter_actions(ws, "proj-ln", rows, 1, 3,
                                [{"id": "ln:sect", "action": "advance", "note": "探院"}])
    assert rows[1]["progress"].count("1-3 [推]探院") == 1


# ---------------------------------------------------------------- 事件视图

def test_event_view_declared_priority_and_cap(tmp_path):
    rows = _ledger()
    for ln in rows:
        ln["status"], ln["opened"] = "active", {"vol": 1, "ch": 2}
        ln["last_seen"] = {"vol": 1, "ch": 2}
    # 声明优先 + 词元命中（ln:sect desc 提到青云宗/青山镇）
    cards = L.event_view(rows, ["ln:sect"], "陈锋探院，古玉忽然发烫", 1, 5, 20)
    ids = [c.split(" ")[0] for c in cards if c.startswith("ln:")]
    assert "ln:sect" in ids
    assert len(ids) <= L.MAX_EVENT_LINES
    # ≥2 条 → 交织标注
    cards2 = L.event_view(rows, ["ln:main", "ln:sect"], "复仇之夜撞上宗门巡查", 1, 5, 20)
    assert any("交织点" in c for c in cards2)
    # closed 线永不注入
    rows[0]["status"], rows[0]["closed"] = "closed", {"vol": 1, "ch": 1, "reason": "r"}
    cards3 = L.event_view(rows, ["ln:main"], "复仇", 1, 5, 20)
    assert not any(c.startswith("ln:main") for c in cards3)


# ---------------------------------------------------------------- 提名与回写

def test_apply_extracted_rows_close_planned_vs_candidate(tmp_path):
    ws = _ws(tmp_path)
    rows = _ledger(main_vol=1)   # main target 在第 1 卷
    res = L.apply_extracted_rows(ws, "proj-ln", rows, [
        {"id": "ln:main", "action": "close", "note": "弑仇完成"}], 1, 20)
    assert rows[0]["status"] == "closed" and not res["candidates"]
    rows2 = _ledger(main_vol=1)
    res2 = L.apply_extracted_rows(ws, "proj-ln", rows2, [
        {"id": "ln:sect", "action": "close", "note": "被剧情带着强行收束"}], 2, 3)
    # target.vol=1 ≠ 当前卷 2 → 计划外候选，status 不变（dormant 未开）
    assert rows2[1]["status"] == "dormant" and rows2[1]["closing_candidate"]["vol"] == 2
    assert res2["candidates"] and "待人工确认" in res2["candidates"][0]


def test_apply_extracted_rows_nominates_unknown_and_dead_revival(tmp_path):
    ws = _ws(tmp_path)
    rows = _ledger()
    res = L.apply_extracted_rows(ws, "proj-ln", rows, [
        {"id": "ln:fresh", "action": "open", "note": "新冒出的暗线"}], 1, 3)
    fresh = next(x for x in rows if x["id"] == "ln:fresh")
    assert fresh["status"] == "pending" and res["pending"]     # 模型没有开线权
    assert not fresh.get("opened")
    # 已闭合线再推动 → 死线复活拦截
    rows[1]["status"], rows[1]["closed"] = "closed", {"vol": 1, "ch": 2, "reason": "r"}
    res2 = L.apply_extracted_rows(ws, "proj-ln", rows, [
        {"id": "ln:sect", "action": "advance", "note": "z"}], 1, 4)
    assert any("死线复活" in w for w in res2["warnings"])
    # pending 提名 → 人工转正 → dormant
    assert L.confirm_pending(rows, "ln:fresh", vol=1, ch=4)
    assert next(x for x in rows if x["id"] == "ln:fresh")["status"] == "dormant"
    assert not L.confirm_pending(rows, "ln:fresh", vol=1, ch=5)  # 二次转正无效


def test_parse_line_rows_and_decl():
    content = ("事件：陈锋夜探宗门 | discovery | 陈锋\n"
               "线索：ln:sect | 推 | 陈锋探院无实据\n"
               "线索：ln:main | 交织 | 与宗门线相撞\n"
               "线索：ln:sect | 乱写 | 坏行\n")
    rows = L.parse_line_rows(content)
    assert [r["action"] for r in rows] == ["advance", "weave"]
    assert rows[0]["id"] == "ln:sect" and rows[0]["note"] == "陈锋探院无实据"
    # 无 ln: 前缀自动补
    rows2 = L.parse_line_rows("线索：sect | 开 | 启动")
    assert rows2[0]["id"] == "ln:sect" and rows2[0]["action"] == "open"
    # 细纲行内声明
    decl = L.parse_line_decl(
        "key_events: [a]\n"
        "本章线索: [{\"id\": \"ln:sect\", \"action\": \"advance\", \"note\": \"n\"}]\n")
    assert decl == [{"id": "ln:sect", "action": "advance", "note": "n"}]
    assert L.parse_line_decl("无线索行") == []


# ---------------------------------------------------------------- 规划侧（forge）

def _blank_bp():
    from novelist.forge.state import Blueprint

    meta = {"title": "t", "genre": "g", "logline": "l",
            "scale": {"volumes": 2, "chapters_per_volume": 10,
                      "target_words_per_chapter": 2000}}
    return Blueprint.blank(meta)


def test_apply_lines_skeleton_main_unique_hard_gate(tmp_path):
    from novelist.forge.nodes import _apply_lines_skeleton

    bp = _blank_bp()
    warns = _apply_lines_skeleton(bp, [
        {"id": "ln:m", "desc": "主线", "kind": "main", "target": {"vol": 1, "note": "n"}},
        {"id": "ln:s", "desc": "支线", "kind": "subplot"}])
    assert bp.find_by_id("lines", "ln:m") is not None
    assert bp.find_by_id("lines", "ln:s")["status"] == "dormant"
    with pytest.raises(ValueError, match="主线不唯一"):
        _apply_lines_skeleton(bp, [
            {"id": "ln:m2", "desc": "第二条主线", "kind": "main",
             "target": {"vol": 1, "note": "n"}}])
    # main 缺 target → 占位 + 告警
    bp2 = _blank_bp()
    warns2 = _apply_lines_skeleton(bp2, [{"id": "ln:m", "desc": "主线", "kind": "main"}])
    assert bp2.find_by_id("lines", "ln:m")["target"]["vol"] == 2
    assert any("target" in w for w in warns2)


def test_sync_bible_exports_lines_with_runtime_merge(tmp_path):
    from novelist.forge.nodes import sync_bible

    ws = _ws(tmp_path)
    # 盘上先有运行态（open 过的支线 + pending 提名行）
    disk = _ledger()
    disk[1]["status"], disk[1]["opened"] = "active", {"vol": 1, "ch": 2}
    disk.append(L.new_line("ln:fresh", "提名线", status="pending"))
    L.save_lines(ws, "proj-ln", disk)
    # 蓝图骨架（status 蓝图值 dormant 不得覆盖盘上 active）
    bp = _blank_bp()
    bp.data["lines"] = [
        {"id": "ln:main", "desc": "主角向青云宗复仇", "kind": "main",
         "target": {"vol": 2, "note": "弑仇"}, "status": "dormant"},
        {"id": "ln:sect", "desc": "青云宗暗查青山镇", "kind": "subplot",
         "target": {"vol": 1, "note": "执法堂立项"}, "status": "dormant"},
        {"id": "ln:jade", "desc": "古玉低语", "kind": "hidden", "status": "dormant"}]
    written = sync_bible(ws, "proj-ln", bp)
    assert "bible/lines.json" in written
    out = {x["id"]: x for x in L.load_lines(ws, "proj-ln")}
    assert out["ln:sect"]["status"] == "active"        # 运行态胜
    assert out["ln:fresh"]["status"] == "pending"      # 盘上独有行保留
    assert out["ln:main"]["target"]["note"] == "弑仇"   # 应然骨架正常导出


def test_lines_ledger_falls_back_to_blueprint(tmp_path):
    """构建期账本未落盘时退回蓝图骨架段（bible 文件优先）。"""
    from novelist.forge.nodes import _lines_ledger

    bp = _blank_bp()
    bp.data["lines"] = [{"id": "ln:m", "desc": "主线", "kind": "main",
                         "target": {"vol": 1, "note": "n"}}]

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.ws = None
    ctx.project_id = "proj-none"
    ctx.bp = bp
    rows = _lines_ledger(ctx)
    assert [x["id"] for x in rows] == ["ln:m"]


def test_render_gist_md_carries_lines_present(tmp_path):
    from novelist.forge.nodes import render_gist_md

    gist = {"vol": 1, "ch": 2, "title": "t", "key_events": ["e1"],
            "lines_present": [{"id": "ln:sect", "action": "advance", "note": "探院"}]}
    md = render_gist_md(gist, 1, 2, ["陈锋"])
    fm = json.loads(md.split("---\n")[1])
    assert fm["lines_present"] == [{"id": "ln:sect", "action": "advance", "note": "探院"}]
    assert "本章线索:" in md
    assert L.parse_line_decl(md)[0]["id"] == "ln:sect"


# ---------------------------------------------------------------- 生成侧注入

def test_prompt_budget_pins_lines():
    from novelist.core.prompt_budget import EVICT_PRIORITY, apply_prompt_budget

    assert EVICT_PRIORITY["lines"] == -1
    sections = [("goal", "x" * 100), ("related:thread", "y" * 100), ("lines", "z" * 100)]
    kept, evicted = apply_prompt_budget(sections, 150)
    assert "lines" not in evicted and "related:thread" in evicted


def test_context_injects_word_floor(tmp_path):
    """字数下限注入（2026-09-06 拍板）：下限 = 目标 85%，只进 user_goal 不进 system。"""
    from novelist.core.context import build_chapter_context, build_system_prompt

    ws = _ws(tmp_path)
    # 注意 bible_path 会补 .json 后缀，name 传 "style" 而非 "style.json"
    ws.write_json(ws.bible_path("proj-ln", "style"),
                  {"target_words_per_chapter": 2400})
    out = build_chapter_context(ws, "proj-ln", 1, 1, gist_text="")
    assert "不少于 2040 字" in out.user_goal          # 2400 * 0.85
    assert "下限不是目标" in out.user_goal and "严禁注水独白" in out.user_goal
    bible = {"worldview": {}, "characters": [],
             "style": {"target_words_per_chapter": 2400}}
    sys_out = build_system_prompt(bible, [], 1, 1)
    assert "2400" not in sys_out and "2040" not in sys_out
    # 无字数配置 → 不注入
    ws2 = _ws(tmp_path / "w2")
    out2 = build_chapter_context(ws2, "proj-ln", 1, 1, gist_text="")
    assert "不少于" not in out2.user_goal


def test_event_goal_emits_line_cards():
    from novelist.core.orchestrator import _event_goal

    out = _event_goal("goal", "事件", 1, 2, "", 200, [], True,
                      line_cards=["ln:sect 支线·active | 上次1-2(距1章) | 1-2密报 | 本章:推进"])
    assert "【本事件线索卡】" in out and "ln:sect" in out
    out2 = _event_goal("goal", "事件", 1, 2, "", 200, [], True)
    assert "【本事件线索卡】" not in out2


def test_schema_files_valid():
    import jsonschema

    schema = json.loads(
        Path("schemas/bible/lines.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    v = jsonschema.Draft202012Validator(schema)
    row = L.new_line("ln:main", "主线", kind="main", target={"vol": 1, "note": "n"})
    assert not list(v.iter_errors([row]))
    bad = dict(row)
    bad["status"] = "nope"
    assert list(v.iter_errors([bad]))
