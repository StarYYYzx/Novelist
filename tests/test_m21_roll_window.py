"""M3 未来窗口滚动细纲（docs/10 §7.7 可变层）测试：engine.roll_window + CLI。

覆盖（**单测绝不真调 LLM**，docs/09 §2.1——用 ScriptedProvider 造 chapter 节点）：
- 自动落盘：未触及承诺 → 未来窗口 key_events 重生成并落盘（细纲 md + 蓝图 chapters）
- 起点自动定位：from_ch 缺省 = 卷内第一未写章
- 已写章跳过 / 窗口越过卷尾钳制 / 整卷写完 → 无未来窗口提示（零 LLM）
- 门控安全：gate=True 且纯 key_events 修订不误触发 mark_pending
- 触及承诺（确定性门）：touched_entries 判定真触发（抵触路径由 promise 门兜底）
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from novelist.cli import cli
from novelist.core.bible import parse_gist
from novelist.forge import Blueprint, roll_window
from novelist.forge.engine import build
from novelist.forge.genres import load_pack_for
from novelist.forge.seed import _init_blueprint, _parse_seed_spec
from novelist.providers.fake import ScriptedProvider

SEED_META = {"title": "t", "genre": "修仙", "logline": "x",
             "scale": {"volumes": 2, "chapters_per_volume": 2, "target_words_per_chapter": 100}}


def _reply(artifact, decide="done", children=None, reason="测试脚本固定回复"):
    return {"final": json.dumps({"artifact": artifact, "decide": decide, "reason": reason,
                                 "children": children or []}, ensure_ascii=False)}


def _init_bp(ws, pid):
    spec = _parse_seed_spec(json.dumps({
        "genre": "修仙", "template_suggestion": "修仙男频", "logline": "五五开系统",
        "protagonist_hint": {"name": "叶蓝", "gender": "male", "cheat": "五五开系统"},
        "scale_hint": {"volumes": 2, "chapters_per_volume": 2},
    }, ensure_ascii=False))
    bp = _init_blueprint(ws, pid, "测试", spec, load_pack_for("修仙男频"), "修仙男频",
                         2, 2, 1000)
    bp.save(ws, pid)
    return bp


BOOK_ART = {
    "worldview": {"name": "落霞界", "power_system": {"mechanic": "五五开", "levels": ["练气", "筑基"]},
                  "rules": ["铁律"], "factions": ["落霞宗"]},
    "characters": [{"id": "char:protagonist", "name": "叶蓝", "gender": "male", "role": "protagonist",
                    "core_traits": ["谨慎"], "power": {"level": "凡人", "faction": "落霞宗"},
                    "first_appear": {"vol": 1, "ch": 1}}],
    "volumes": [{"vol": 1, "title": "V1", "summary": "一卷主线", "key_beats": ["k"]},
                {"vol": 2, "title": "V2", "summary": "二卷主线", "key_beats": ["k"]}],
    "threads": [{"id": "pt:yuwen", "desc": "玉牌之谜", "scope": "volume", "target_vol": 1},
                {"id": "pt:guwu", "desc": "故人消息", "scope": "volume", "target_vol": 2}],
}


def _chapter(events, ch, turns=None, threads=None, characters=None):
    return _reply({"title": f"章{ch}", "pov": "第三人称限知（主角视角）",
                   "key_events": events, "turns": turns or [f"转折{ch}"],
                   "characters": characters or ["char:protagonist"],
                   "threads_involved": threads or ["pt:yuwen"],
                   "after_days": 0})


_WV_EXPAND_CHILDREN = [{"id": "sys1", "brief": "力量体系细化", "focus": "等级与资源"},
                       {"id": "sys2", "brief": "势力地理", "focus": "宗门分布"}]


def _chapter_script():
    """合法项目全脚本。引擎执行顺序：vol1 → chapter 1-1/1-2 → vol2（卷闸门），
    脚本必须按实际消费顺序排（F4 教训）。"""
    return [
        _reply(BOOK_ART),
        _reply({"name": "落霞界"}, "expand", _WV_EXPAND_CHILDREN),
        _reply({"title": "力量体系", "kind": "power",
                "settings": [{"id": "set:wuwei", "keywords": ["无为"], "text": "无为诀可共享修为。"}]}),
        _reply({"title": "势力地理", "kind": "geo",
                "settings": [{"id": "set:luoxia", "keywords": ["落霞"], "text": "落霞宗居落霞峰。"}]}),
        _reply(None, "done"),
        _reply({"narration": "白描", "forbidden_words": ["打卡"], "glossary": [{"term": "五五开", "note": "共享"}]}),
        _reply({"threads": [{"id": "pt:yuwen", "desc": "玉牌之谜", "scope": "volume", "target_vol": 1,
                             "plant_desc": "第 1 章遗物现世", "payoff_desc": "卷末揭秘"},
                            {"id": "pt:guwu", "desc": "故人消息", "scope": "volume", "target_vol": 2,
                             "plant_desc": "第 3 章来信", "payoff_desc": "卷末见面"}]}),
        _reply({"vol": 1, "title": "V1", "summary": "一卷主线", "key_beats": ["k"],
                "threads_to_payoff": ["pt:yuwen"]}),
        # 事件先行（2026-09-19）：卷一产事件流（2 条 → 切 2 章），章细纲由切片物化
        _reply({"events": [
            {"desc": "事件1", "scene": "场景1", "pov": "第三人称限知（主角视角）",
             "days": 0, "est_words": 800, "climax": True,
             "characters": ["char:protagonist"], "threads_involved": ["pt:yuwen"],
             "beads": {"lines": []}},
            {"desc": "事件2", "scene": "场景2", "pov": "第三人称限知（主角视角）",
             "days": 1, "est_words": 800, "climax": True,
             "characters": ["char:protagonist"], "threads_involved": ["pt:yuwen"],
             "beads": {"lines": []}},
        ]}),
        _reply({"vol": 2, "title": "V2", "summary": "二卷主线", "key_beats": ["k"],
                "threads_to_payoff": ["pt:guwu"]}),
    ]


def _build_ok(ws, pid):
    r = build(ws, pid, provider=ScriptedProvider(_chapter_script()), max_calls=60, gate=False)
    assert r.ok, r.warnings
    return r


def _mark_written(ws, pid, vol, ch):
    p = ws.chapter_path(pid, vol, ch)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"第 {ch} 章正文（占位）", encoding="utf-8")


# ---- 自动落盘：未触及承诺，未来窗口 key_events 重生成并落盘 ----
def test_auto_persist_regenerates_future_window(ws_factory):
    ws, pid = ws_factory("proj-rw-auto")
    _init_bp(ws, pid)
    _build_ok(ws, pid)
    _mark_written(ws, pid, 1, 1)  # 1-1 已成文，未来窗口自 1-2 起

    res = roll_window(ws, pid, provider=ScriptedProvider([_chapter(["事件2-修订"], 2)]),
                      vol=1, from_ch=2, width=2, gate=True)
    assert res.ok
    assert res.gate_halted is False
    assert res.width_applied == 1
    assert res.start_ch == 2

    # 细纲 md 与蓝图均已更新为修订后 key_events
    g = parse_gist(ws, pid, 1, 2)
    assert g["key_events"] == ["事件2-修订"], g.get("key_events")
    g_text = ws.outline_chapter_path(pid, 1, 2).read_text(encoding="utf-8")
    assert "事件2-修订" in g_text
    bp = Blueprint.load(ws, pid)
    chap2 = next(x for x in bp.section("chapters") if x.get("ch") == 2)
    assert chap2["key_events"] == ["事件2-修订"]


# ---- gate=True 且纯 key_events 修订：承诺门不误触发（不落 pending）----
def test_gate_no_false_trigger(ws_factory):
    ws, pid = ws_factory("proj-rw-gate")
    _init_bp(ws, pid)
    _build_ok(ws, pid)

    res = roll_window(ws, pid, provider=ScriptedProvider([
        _chapter(["事件1-修订"], 1), _chapter(["事件2-修订"], 2)]),
        vol=1, from_ch=1, width=3, gate=True)
    assert res.ok
    assert res.gate_halted is False
    assert not res.pending_review and not res.touched, (res.pending_review, res.touched)
    # threads/volumes/characters 承诺守卫字段未被窗口修订改动
    bp = Blueprint.load(ws, pid)
    assert next(t for t in bp.section("threads") if t["id"] == "pt:yuwen")["status"] \
        in ("planted", "unplanned")


# ---- 起点自动定位 + 越卷尾钳制 ----
def test_default_start_first_unwritten_and_tail_clamp(ws_factory):
    ws, pid = ws_factory("proj-rw-default")
    _init_bp(ws, pid)
    _build_ok(ws, pid)

    # 无成文 → start=1，窗口 width=3 被 K=2 钳制为 [1,2]
    res = roll_window(ws, pid, provider=ScriptedProvider([
        _chapter(["事件1-修订"], 1), _chapter(["事件2-修订"], 2)]),
        vol=1, width=3, gate=False)
    assert res.ok
    assert res.start_ch == 1
    assert res.width_applied == 2
    assert res.calls_used == 2  # 每章一次 LLM（无 retry）


# ---- 已写章跳过：窗口只剩未写章 ----
def test_skip_written_chapters(ws_factory):
    ws, pid = ws_factory("proj-rw-skip")
    _init_bp(ws, pid)
    _build_ok(ws, pid)
    _mark_written(ws, pid, 1, 1)  # 1-1 已写

    res = roll_window(ws, pid, provider=ScriptedProvider([_chapter(["事件2-修订"], 2)]),
                      vol=1, from_ch=1, width=3, gate=False)
    assert res.ok
    assert res.width_applied == 1
    assert res.start_ch == 1  # 起点 1，但 1-1 已写被跳过，只滚 1-2
    g = parse_gist(ws, pid, 1, 1)
    assert g["key_events"] == ["事件1"]  # 已写章未被改


# ---- 整卷已写完：无未来窗口 → 零 LLM，提示 roll 下一卷 ----
def test_volume_done_no_future_window(ws_factory):
    ws, pid = ws_factory("proj-rw-done")
    _init_bp(ws, pid)
    _build_ok(ws, pid)
    _mark_written(ws, pid, 1, 1)
    _mark_written(ws, pid, 1, 2)

    class _Bomb:
        """被调用即失败——证明无未来窗口时绝不调 LLM。"""
        def complete(self, req):
            raise AssertionError("volume 已写完，不应调用 LLM")

        @property
        def capabilities(self):
            from novelist.core.llm import ProviderCapabilities
            return ProviderCapabilities(tool_calling=True, max_context=0,
                                        json_mode=True, streaming=False, embedding=False)

    res = roll_window(ws, pid, provider=_Bomb(), vol=1, from_ch=1, gate=True)
    assert res.ok
    assert res.width_applied == 0
    assert any("forge roll 2" in w for w in res.warnings), res.warnings


# ---- 触及承诺（确定性门）：touched_entries 守卫生效 ----
def test_covenant_touch_triggers_gate(ws_factory):
    ws, pid = ws_factory("proj-rw-touch")
    _init_bp(ws, pid)
    _build_ok(ws, pid)

    from novelist.forge.covenant import build_covenant, touched_entries

    # 改动前快照 = 刚构建完成的蓝图；让 pt:yuwen 处于"在途承诺"态（planted）做基线
    bp_pre = Blueprint.load(ws, pid)
    for t in bp_pre.section("threads"):
        if t.get("id") == "pt:yuwen":
            t["status"] = "planted"
    covenant = build_covenant(bp_pre)
    assert any(e.obj_id == "pt:yuwen" for e in covenant)

    # 模拟窗口修订过程中该伏笔被提前兑付（status → returned，guard 字段变化）
    bp_now = Blueprint.load(ws, pid)
    for t in bp_now.section("threads"):
        if t.get("id") == "pt:yuwen":
            t["status"] = "returned"

    touched = touched_entries(bp_pre, bp_now, covenant)
    assert any(e.obj_id == "pt:yuwen" for e in touched), \
        "承诺守卫应检测到伏笔 status 变更"


# ---- CLI：forge roll-window 输出与标记 ----
def test_cli_roll_window_ok(ws_factory):
    ws, pid = ws_factory("proj-rw-cli")
    _init_bp(ws, pid)
    _build_ok(ws, pid)
    _mark_written(ws, pid, 1, 1)

    # CLI 用真 provider，无法全调用 job——只验证命令注册与帮助文案暴露功能
    runner = CliRunner()
    result = runner.invoke(cli, ["forge", "roll-window", "--help"])
    assert result.exit_code == 0
    assert "未来窗口" in result.output
    assert "forge roll" in result.output