"""M3l F4 测试（docs/10 §7 / docs/08 F4）：旁支递归深化 + arc/beat 层 + forge roll。

覆盖（**单测绝不真调 LLM**，docs/09 §2.1——全部用 ScriptedProvider）：
- 旁支 DFS：worldview expand → system（settings 落库）→ setting_entry；character_group/
  style/thread_set 节点落 bible；nodes/<id>.json 增量落盘 + resume 跳过（幂等）
- arc 层：volume expand → arc 节点 → outline/arcs.json；beat 层：chapter expand →
  gist.beats 进细纲 front-matter；max_width 截断告警
- roll：§7.7 四块注入（roll_context 进 volume prompt）→ vol2 细纲 → worldstate pending
  幂等追加（不覆盖 time/characters）；前置校验（vol=1 拒绝 / 前卷无正文拒绝）；CLI 接线
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from novelist.cli import cli
from novelist.core.bible import parse_gist
from novelist.forge.engine import build, roll
from novelist.forge.genres import load_pack_for
from novelist.forge.seed import _init_blueprint, _parse_seed_spec
from novelist.providers.fake import ScriptedProvider

SEED_META = {"title": "t", "genre": "修仙", "logline": "x",
             "scale": {"volumes": 2, "chapters_per_volume": 2, "target_words_per_chapter": 100}}


def _reply(artifact, decide="done", children=None, reason="测试脚本固定回复"):
    return {"final": json.dumps({"artifact": artifact, "decide": decide, "reason": reason,
                                 "children": children or []}, ensure_ascii=False)}


def _init_bp(ws, pid, volumes=2, chapters=2):
    spec = _parse_seed_spec(json.dumps({
        "genre": "修仙", "template_suggestion": "修仙男频", "logline": "五五开系统",
        "protagonist_hint": {"name": "叶蓝", "gender": "male", "cheat": "五五开系统"},
        "scale_hint": {"volumes": volumes, "chapters_per_volume": chapters},
    }, ensure_ascii=False))
    bp = _init_blueprint(ws, pid, "测试", spec, load_pack_for("修仙男频"), "修仙男频",
                         volumes, chapters, 1000)
    bp.save(ws, pid)
    return bp


BOOK_ART = {
    "worldview": {"name": "落霞界", "power_system": {"mechanic": "五五开", "levels": ["练气", "筑基"]},
                  "rules": ["铁律"], "factions": ["落霞宗"]},
    "characters": [{"id": "char:yelan", "name": "叶蓝", "gender": "male", "role": "protagonist",
                    "core_traits": ["谨慎"], "power": {"level": "凡人", "faction": "落霞宗"},
                    "first_appear": {"vol": 1, "ch": 1}}],
    "volumes": [{"vol": 1, "title": "V1", "summary": "一卷主线", "key_beats": ["k"]},
                {"vol": 2, "title": "V2", "summary": "二卷主线", "key_beats": ["k"]}],
    "threads": [{"id": "pt:yuwen", "desc": "玉牌之谜", "scope": "volume", "target_vol": 1}],
}
WV_EXPAND_CHILDREN = [{"id": "sys1", "brief": "力量体系细化", "focus": "等级与资源"},
                      {"id": "sys2", "brief": "势力地理", "focus": "宗门分布"}]


def _deepen_script(chapters=2):
    """deepen build 全脚本：book → worldview(expand) → system×2 → character_group →
    style → thread_set → volume → chapter×2。"""
    return [
        _reply(BOOK_ART),
        # worldview：expand 出 2 个 system
        _reply({"name": "落霞界"}, "expand", WV_EXPAND_CHILDREN),
        # system ×2（settings 落库；不再 expand）
        _reply({"title": "力量体系", "kind": "power",
                "settings": [{"id": "set:wuwei", "keywords": ["无为"], "text": "无为诀可共享修为。"}]}),
        _reply({"title": "势力地理", "kind": "geo",
                "settings": [{"id": "set:luoxia", "keywords": ["落霞"], "text": "落霞宗居落霞峰。"}]}),
        # character_group：done（骨架已够）
        _reply(None, "done"),
        # style
        _reply({"narration": "白描", "forbidden_words": ["打卡"], "glossary": [{"term": "五五开", "note": "共享"}]}),
        # thread_set
        _reply({"threads": [{"id": "pt:yuwen", "desc": "玉牌之谜", "scope": "volume", "target_vol": 1,
                             "plant_desc": "第 1 章遗物现世", "payoff_desc": "卷末揭秘"}]}),
        # volume 1 →（卷闸门）chapter 1-1/1-2 → volume 2（引擎实际消费顺序）
        _reply({"vol": 1, "title": "V1", "summary": "一卷主线", "key_beats": ["k"]}),
        *[ _reply({"title": f"章{c}", "pov": "第三人称限知（主角视角）",
                   "key_events": [f"事件{c}"], "turns": [f"转折{c}"],
                   "characters": ["char:yelan"], "after_days": 0}) for c in range(1, chapters + 1) ],
        _reply({"vol": 2, "title": "V2", "summary": "二卷主线", "key_beats": ["k"]}),
    ]


# ---- 旁支 DFS ----
def test_deepen_side_branches(ws_factory, capsys):
    ws, pid = ws_factory("proj-f4a")
    _init_bp(ws, pid)
    r = build(ws, pid, provider=ScriptedProvider(_deepen_script()), max_calls=60, gate=False)
    assert r.ok, r.warnings
    # 旁支调用计数：book1 + worldview1 + system2 + cg1 + style1 + ts1 + vol2 + ch2 = 11
    assert r.calls_used == 11, r.calls_used
    # settings 落库（system 产物）
    settings = json.loads(ws.bible_path(pid, "settings").read_text(encoding="utf-8"))
    ids = {s["id"] for s in settings}
    assert {"set:wuwei", "set:luoxia"} <= ids
    # style 深化落库
    style = json.loads(ws.bible_path(pid, "style").read_text(encoding="utf-8"))
    assert "打卡" in (style.get("forbidden_words") or [])
    assert any(g.get("term") == "五五开" for g in (style.get("glossary") or []))
    # thread_set 深化落库
    threads = json.loads(ws.bible_path(pid, "plot_threads").read_text(encoding="utf-8"))
    assert any(t.get("plant_desc") for t in threads)
    # nodes/ 增量落盘（docs/10 §7.4）
    nodes_dir = ws._abs(f"{pid}/workspace/forge/nodes")
    names = {p.name for p in nodes_dir.glob("*.json")}
    assert {"book.json", "worldview.json", "system-sys1.json", "system-sys2.json",
            "character_group.json", "style.json", "thread_set.json"} <= names
    # 节点产物含 decide/reason（构建报告审计用）
    wv = json.loads((nodes_dir / "worldview.json").read_text(encoding="utf-8"))
    assert wv["decide"] == "expand" and len(wv["reason"]) > 0
    capsys.readouterr()


def test_deepen_resume_skips_done_nodes(ws_factory, capsys):
    ws, pid = ws_factory("proj-f4r")
    _init_bp(ws, pid)
    r1 = build(ws, pid, provider=ScriptedProvider(_deepen_script()), max_calls=60, gate=False)
    assert r1.ok
    n1 = r1.calls_used
    # resume：nodes/ + 产物齐备 → 零新调用
    r2 = build(ws, pid, provider=ScriptedProvider([]), max_calls=60, resume=True)
    assert r2.calls_used == n1
    capsys.readouterr()


# ---- arc / beat 层 ----
def test_arc_and_beat_layers(ws_factory, capsys):
    ws, pid = ws_factory("proj-f4b")
    _init_bp(ws, pid)
    script = _deepen_script()
    # volume 1 改为 expand 出 1 个 arc
    script[7] = _reply({"vol": 1, "title": "V1", "summary": "一卷主线"}, "expand",
                       [{"id": "arc-1", "brief": "立足宗门", "focus": "外门试炼"}])
    # chapter 1-1 改为 expand（触发 beat）
    script[9] = _reply({"title": "章1", "pov": "第三人称限知（主角视角）",
                        "key_events": ["事件1"], "turns": ["转折1"],
                        "characters": ["char:yelan"], "after_days": 0},
                       "expand", [{"id": "beat1", "brief": "玉牌认主的节拍"}])
    # beat 回复必须插在 chapter 1-1 之后、1-2 之前（引擎 DFS 顺序）
    script.insert(10, _reply({"beats": ["拍1：拾玉（低）", "拍2：认主（高）"]}))
    r = build(ws, pid, provider=ScriptedProvider(script), max_calls=60, gate=False)
    assert r.ok, r.warnings
    # arc 落盘（自定决策：outline/arcs.json 独立文件）
    arcs = json.loads(ws._abs(f"{pid}/outline/arcs.json").read_text(encoding="utf-8"))
    assert [a["id"] for a in arcs] == ["arc:arc-1"] and arcs[0]["vol"] == 1
    assert arcs[0]["brief"] == "立足宗门"
    # beat 并入细纲（自定决策：gist.beats 进 front-matter + 节拍行）
    md = ws.outline_chapter_path(pid, 1, 1).read_text(encoding="utf-8")
    gist = parse_gist(ws, pid, 1, 1)
    assert gist["beats"] == ["拍1：拾玉（低）", "拍2：认主（高）"]
    assert "拍2：认主（高）" in md and "beats" in md
    # nodes 落盘
    nodes_dir = ws._abs(f"{pid}/workspace/forge/nodes")
    assert (nodes_dir / "arc-1-arc-1.json").exists()
    assert (nodes_dir / "beat-1-1.json").exists()
    capsys.readouterr()


def test_max_width_truncation_warning(ws_factory, capsys):
    ws, pid = ws_factory("proj-f4w")
    _init_bp(ws, pid)
    script = _deepen_script()
    # worldview expand 给 6 个 children（> max_width=4）→ 截断告警，system 只跑 4 次
    script[1] = _reply({"name": "落霞界"}, "expand",
                       [{"id": f"sys{i}", "brief": f"维度{i}"} for i in range(1, 7)])
    for i in range(1, 5):
        script[1 + i] = _reply({"title": f"d{i}", "kind": "power",
                                "settings": [{"id": f"set:d{i}", "keywords": ["k"], "text": "t"}]})
    r = build(ws, pid, provider=ScriptedProvider(script), max_calls=60, gate=False,
              max_width_list=3)  # ADR-033 A：宽类压到 3，6 个子节点仍被截断
    assert r.ok
    assert any("超出 max_width=3" in w for w in r.warnings)
    capsys.readouterr()


# ---- forge roll ----
def _setup_rolled_project(ws, pid):
    """前卷已有正文 + 记忆事件 + worldstate 现状的 roll 前置。"""
    bp = _init_bp(ws, pid)
    # 前卷（vol1）正文 2 章
    for ch in (1, 2):
        p = ws.chapter_path(pid, 1, ch)
        p.parent.mkdir(parents=True, exist_ok=True)
        ws.write_text(p, f"# 第 {ch} 章 前卷正文\n\n叶蓝修行。{'落霞宗' * 30}\n")
    # 前卷记忆事件（roll 注入块 2）
    ws.write_json(ws._abs(f"{pid}/memory/plot_events.json"), [
        {"id": f"ev:{pid}:1:1:1", "at": {"vol": 1, "ch": 1}, "type": "discovery",
         "summary": "叶蓝拾玉", "participants": ["char:yelan"], "affected_threads": []},
        {"id": f"ev:{pid}:1:2:1", "at": {"vol": 1, "ch": 2}, "type": "turning_point",
         "summary": "玉牌认主", "participants": ["char:yelan"], "affected_threads": ["pt:yuwen"]},
    ])
    # worldstate 现状（roll 注入块 3 + 追加基准）
    ws.write_json(ws.bible_path(pid, "worldstate"), {
        "time": {"now": 30, "origin_text": "穿越之日"},
        "pending": [],
        "characters": {"char:yelan": {"name": "叶蓝", "realm": "练气三层", "location": "落霞宗",
                                      "items": [], "injuries": [], "dead": False, "history": []}},
    })
    return bp


def _roll_script():
    """roll 脚本：volume2(expand 1 arc) → arc → chapter 2-1 / 2-2。"""
    return [
        _reply({"vol": 2, "title": "V2", "summary": "外域历练", "key_beats": ["k"],
                "threads_to_payoff": ["pt:yuwen"]}, "expand",
               [{"id": "arc-1", "brief": "历练启程", "focus": "坊市风波"}]),
        _reply({"title": "历练启程", "brief": "坊市风波", "focus": "外门",
                "chapters_hint": "第 1–2 章"}),
        _reply({"title": "章2-1", "pov": "第三人称限知（主角视角）", "key_events": ["进入外域"],
                "turns": ["遇袭"], "characters": ["char:yelan"], "after_days": 7}),
        _reply({"title": "章2-2", "pov": "第三人称限知（主角视角）", "key_events": ["坊市夺宝"],
                "turns": ["反转"], "characters": ["char:yelan"], "after_days": 3}),
    ]


def test_roll_generates_vol2_with_context(ws_factory, capsys):
    ws, pid = ws_factory("proj-f4c")
    _setup_rolled_project(ws, pid)
    # 捕获 volume prompt（验证 §7.7 四块注入）
    captured = {}
    real_complete = ScriptedProvider.complete

    def spy_complete(self, req):
        if "卷大纲师" in (req.messages[1].content or ""):
            captured["prompt"] = req.messages[1].content
        return real_complete(self, req)

    provider = ScriptedProvider(_roll_script())
    ScriptedProvider.complete = spy_complete
    try:
        # gate=False：本测试验证四块上下文注入与生成（闸门行为在 test_flow_fixes 覆盖）
        r = roll(ws, pid, provider=provider, vol=2, max_calls=40, gate=False)
    finally:
        ScriptedProvider.complete = real_complete
    assert r.ok, r.warnings
    assert r.calls_used == 4 and r.chapters_written == 2 and r.arcs_written == 1
    # 四块上下文确实进了 volume prompt
    prompt = captured.get("prompt") or ""
    assert "前卷主线" in prompt and "前卷末 3 章实际发生" in prompt
    assert "世界现状" in prompt and "收尾清单" in prompt
    assert "玉牌认主" in prompt and '"day": 30' in prompt
    # vol2 细纲落盘 + prev 衔接前卷末章
    gist = parse_gist(ws, pid, 2, 1)
    assert gist["key_events"] == ["进入外域"]
    assert gist["after_days"] == 7
    # arc 落盘
    arcs = json.loads(ws._abs(f"{pid}/outline/arcs.json").read_text(encoding="utf-8"))
    assert any(a["vol"] == 2 for a in arcs)
    # worldstate：time/characters 不被覆盖，pending 幂等追加（due = now + 累计）
    wdata = json.loads(ws.bible_path(pid, "worldstate").read_text(encoding="utf-8"))
    assert wdata["time"]["now"] == 30
    assert wdata["characters"]["char:yelan"]["realm"] == "练气三层"
    pend = {p["id"]: p for p in wdata["pending"]}
    assert pend["pd:ke-2-1"]["due"] == 30 + 7
    assert pend["pd:ke-2-2"]["due"] == 30 + 7 + 3
    assert all(p["status"] == "scheduled" for p in wdata["pending"])
    capsys.readouterr()


def test_roll_idempotent_pending(ws_factory, capsys):
    ws, pid = ws_factory("proj-f4i")
    _setup_rolled_project(ws, pid)
    r1 = roll(ws, pid, provider=ScriptedProvider(_roll_script()), vol=2)
    assert r1.ok
    w1 = json.loads(ws.bible_path(pid, "worldstate").read_text(encoding="utf-8"))
    r2 = roll(ws, pid, provider=ScriptedProvider(_roll_script()), vol=2)
    assert r2.ok
    w2 = json.loads(ws.bible_path(pid, "worldstate").read_text(encoding="utf-8"))
    assert len(w2["pending"]) == len(w1["pending"])  # 幂等：不重复追加
    capsys.readouterr()


def test_roll_rejects_vol1_and_missing_prev(ws_factory):
    ws, pid = ws_factory("proj-f4x")
    _init_bp(ws, pid)
    with pytest.raises(ValueError, match="从第 2 卷起"):
        roll(ws, pid, provider=ScriptedProvider([]), vol=1)
    with pytest.raises(ValueError, match="无正文"):
        roll(ws, pid, provider=ScriptedProvider([]), vol=2)


def test_cli_roll(ws_factory, monkeypatch, tmp_path, capsys):
    ws, pid = ws_factory("proj-f4cli")
    _setup_rolled_project(ws, pid)
    monkeypatch.setattr("novelist.cli._make_cli_provider",
                        lambda p, **kw: ScriptedProvider(_roll_script()))
    monkeypatch.chdir(str(ws._abs("")))
    runner = CliRunner()
    result = runner.invoke(cli, ["forge", "roll", "2", pid, "--provider", "fake", "--no-gate"])
    assert result.exit_code == 0, result.output
    assert "roll vol 2 done" in result.output
    assert ws.outline_chapter_path(pid, 2, 2).exists()
