"""M3l F5 测试（docs/10 §9，docs/08 F5）：validate V1–V6 + 报告双写 + 快照/rollback + --diff。

覆盖（**单测绝不真调 LLM**，docs/09 §2.1——构建用 ScriptedProvider，V4 冒烟用 FakeProvider）：
- V2 交叉引用 block：细纲引用不存在的 char id、卷章区间缝隙
- V3 覆盖度 block：主角未在 vol1ch1 出场、settings 低于下限
- V4 可写冒烟：FakeProvider 直出 → ok / bible_injected / cast 非空
- V5/V6 warn 不阻断（合法项目 0 block）
- report 双写：workspace/forge/report.md（全量）+ reports/stats/forge-<ts>.md（摘要）
- CLI：forge validate 通过推进 pipeline 到「细纲」（持久化）；有 block 不推进
- rollback 往返：build 自动快照 → 篡改 → restore 恢复
- --diff：改动 blueprint 实体 → 只重建引用它的章
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from novelist.cli import cli
from novelist.core.bible import parse_gist
from novelist.forge import Blueprint, diff_affected, restore_snapshot
from novelist.forge.engine import build
from novelist.forge.genres import load_pack_for
from novelist.forge.seed import _init_blueprint, _parse_seed_spec
from novelist.forge.validate import validate_project_full
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
    # 主角 id 用 char:protagonist（_slug("叶蓝") 落空 → seed 建档即此 id，upsert 合并成单卡）
    "characters": [{"id": "char:protagonist", "name": "叶蓝", "gender": "male", "role": "protagonist",
                    "core_traits": ["谨慎"], "power": {"level": "凡人", "faction": "落霞宗"},
                    "first_appear": {"vol": 1, "ch": 1}}],
    "volumes": [{"vol": 1, "title": "V1", "summary": "一卷主线", "key_beats": ["k"]},
                {"vol": 2, "title": "V2", "summary": "二卷主线", "key_beats": ["k"]}],
    "threads": [{"id": "pt:yuwen", "desc": "玉牌之谜", "scope": "volume", "target_vol": 1},
                {"id": "pt:guwu", "desc": "故人消息", "scope": "volume", "target_vol": 2}],
}
WV_EXPAND_CHILDREN = [{"id": "sys1", "brief": "力量体系细化", "focus": "等级与资源"},
                      {"id": "sys2", "brief": "势力地理", "focus": "宗门分布"}]


def _build_script():
    """合法项目全脚本。注意引擎执行顺序：vol1 → chapter 1-1/1-2 → vol2（卷闸门）——
    脚本必须按实际消费顺序排，否则回复错位（F4 隐藏 bug，validate 才暴露）。"""
    return [
        _reply(BOOK_ART),
        _reply({"name": "落霞界"}, "expand", WV_EXPAND_CHILDREN),
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
        # vol=1 卷闸门内先展开 chapter 1-1 / 1-2，之后才轮到 vol=2
        *[ _reply({"title": f"章{c}", "pov": "第三人称限知（主角视角）",
                   "key_events": [f"事件{c}"], "turns": [f"转折{c}"],
                   "characters": ["char:protagonist"], "threads_involved": ["pt:yuwen"],
                   "after_days": 0}) for c in range(1, 3) ],
        _reply({"vol": 2, "title": "V2", "summary": "二卷主线", "key_beats": ["k"],
                "threads_to_payoff": ["pt:guwu"]}),
    ]


def _build_ok(ws, pid, max_calls=60):
    r = build(ws, pid, provider=ScriptedProvider(_build_script()), max_calls=max_calls)
    assert r.ok, r.warnings
    return r


# ---- V1/V2/V3 基础：合法项目全过 ----
def test_validate_ok_on_clean_build(ws_factory):
    ws, pid = ws_factory("proj-f5a")
    _init_bp(ws, pid)
    _build_ok(ws, pid)
    res = validate_project_full(ws, pid, settings_min=2)
    assert res.ok, [f.message for f in res.blocks]
    assert not res.smoke_ran  # 未开 smoke 不跑冒烟


def test_v2_block_bad_char_ref(ws_factory):
    ws, pid = ws_factory("proj-f5b")
    _init_bp(ws, pid)
    _build_ok(ws, pid)
    # 篡改细纲：出场人物指向不存在的 id
    p = ws.outline_chapter_path(pid, 1, 1)
    g = parse_gist(ws, pid, 1, 1)
    g["characters"] = ["char:protagonist", "char:nobody"]
    import re
    text = p.read_text(encoding="utf-8")
    text = re.sub(r"---\n.*?\n---", "---\n" + json.dumps(g, ensure_ascii=False) + "\n---",
                  text, count=1, flags=re.S)
    p.write_text(text, encoding="utf-8")
    res = validate_project_full(ws, pid, settings_min=2)
    assert not res.ok
    assert any(f.code == "V2" and "char:nobody" in f.message for f in res.blocks)


def test_v2_block_chapter_gap(ws_factory):
    ws, pid = ws_factory("proj-f5c")
    _init_bp(ws, pid)
    _build_ok(ws, pid)
    # 篡改卷 2 章区间造成缝隙（[3,4] → [4,4]，漏 3）
    vp = ws._abs(f"{pid}/outline/volumes.json")
    vols = json.loads(vp.read_text(encoding="utf-8"))
    vols[1]["chapter_range"] = [4, 4]
    vp.write_text(json.dumps(vols, ensure_ascii=False, indent=2), encoding="utf-8")
    res = validate_project_full(ws, pid, settings_min=2)
    assert not res.ok
    assert any(f.code == "V2" and "缝隙" in f.message for f in res.blocks)


def test_v3_block_protagonist_not_in_ch1(ws_factory):
    ws, pid = ws_factory("proj-f5d")
    _init_bp(ws, pid)
    _build_ok(ws, pid)
    p = ws.outline_chapter_path(pid, 1, 1)
    g = parse_gist(ws, pid, 1, 1)
    g["characters"] = []
    import re
    text = p.read_text(encoding="utf-8")
    text = re.sub(r"---\n.*?\n---", "---\n" + json.dumps(g, ensure_ascii=False) + "\n---",
                  text, count=1, flags=re.S)
    p.write_text(text, encoding="utf-8")
    res = validate_project_full(ws, pid, settings_min=2)
    assert any(f.code == "V3" and "未在卷 1 第 1 章出场" in f.message for f in res.blocks)


def test_v3_block_settings_min(ws_factory):
    ws, pid = ws_factory("proj-f5e")
    _init_bp(ws, pid)
    _build_ok(ws, pid)
    res = validate_project_full(ws, pid, settings_min=100)
    assert any(f.code == "V3" and "settings" in f.message for f in res.blocks)


# ---- V4 冒烟 / V5 V6 warn ----
def test_v4_smoke_ok(ws_factory):
    ws, pid = ws_factory("proj-f5f")
    _init_bp(ws, pid)
    _build_ok(ws, pid)
    res = validate_project_full(ws, pid, smoke=True, settings_min=2)
    assert res.smoke_ran and res.smoke_ok, [f.message for f in res.blocks]
    assert res.ok


def test_v5_v6_warn_does_not_block(ws_factory):
    ws, pid = ws_factory("proj-f5g")
    _init_bp(ws, pid)
    _build_ok(ws, pid)
    res = validate_project_full(ws, pid, settings_min=2)
    assert res.ok  # warn 再多也不阻断
    assert all(f.level == "warn" for f in res.warns)


# ---- 报告双写 ----
def test_report_dual_write(ws_factory):
    ws, pid = ws_factory("proj-f5h")
    _init_bp(ws, pid)
    _build_ok(ws, pid)
    from novelist.forge import write_reports

    full, stats = write_reports(ws, pid)
    assert full.exists() and stats.exists()
    text = full.read_text(encoding="utf-8")
    assert "Forge 构建报告" in text and "校验结果" in text
    assert "来源分布" in text  # provenance 段
    # transcript build_node 事件有 usage 上链（F5b）
    from novelist.forge import read_transcript

    nodes_ev = [r for r in read_transcript(ws, pid) if r.get("event") == "build_node"]
    assert nodes_ev, "transcript 缺 build_node 事件"
    assert all("tokens_in" in r for r in nodes_ev)


# ---- CLI：validate 推进 pipeline（持久化）----
def test_cli_validate_advances_pipeline(ws_factory, monkeypatch):
    ws, pid = ws_factory("proj-f5i")
    _init_bp(ws, pid)
    _build_ok(ws, pid)
    monkeypatch.chdir(str(ws._abs("")))
    runner = CliRunner()
    result = runner.invoke(cli, ["forge", "validate", pid, "--no-report", "--settings-min", "2"])
    assert result.exit_code == 0, result.output
    assert "已推进到「细纲」" in result.output
    project = json.loads(ws._abs(f"{pid}/project.json").read_text(encoding="utf-8"))
    assert project["pipeline_state"] == "细纲"


def test_cli_validate_block_no_advance(ws_factory, monkeypatch):
    ws, pid = ws_factory("proj-f5j")
    _init_bp(ws, pid)
    _build_ok(ws, pid)
    # 先推到细纲，再篡改出 block → 不推进（仍是细纲）
    project_path = ws._abs(f"{pid}/project.json")
    project = json.loads(project_path.read_text(encoding="utf-8"))
    project["pipeline_state"] = "细纲"
    project_path.write_text(json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8")
    p = ws.outline_chapter_path(pid, 1, 1)
    g = parse_gist(ws, pid, 1, 1)
    g["characters"] = ["char:nobody"]
    import re
    text = p.read_text(encoding="utf-8")
    text = re.sub(r"---\n.*?\n---", "---\n" + json.dumps(g, ensure_ascii=False) + "\n---",
                  text, count=1, flags=re.S)
    p.write_text(text, encoding="utf-8")
    monkeypatch.chdir(str(ws._abs("")))
    runner = CliRunner()
    result = runner.invoke(cli, ["forge", "validate", pid, "--no-report", "--settings-min", "2"])
    assert result.exit_code != 0
    assert "阻断项" in result.output
    project = json.loads(project_path.read_text(encoding="utf-8"))
    assert project["pipeline_state"] == "细纲"  # 不推进到正文


# ---- 快照 / rollback ----
def test_build_auto_snapshot_and_rollback(ws_factory):
    ws, pid = ws_factory("proj-f5k")
    _init_bp(ws, pid)
    _build_ok(ws, pid)
    # 双快照语义（F5e）：build 前置快照 + build-ok 结果快照
    from novelist.forge import latest_snapshot, snapshots_dir

    names = sorted(p.name for p in snapshots_dir(ws, pid).iterdir() if p.is_dir())
    assert any(n.endswith("-build") for n in names), names
    assert any(n.endswith("-build-ok") for n in names), names
    # 缺省回滚点 = 最近快照（结果态 build-ok）
    snap = latest_snapshot(ws, pid)
    assert snap is not None and snap.name.endswith("build-ok"), snap
    # 篡改 bible → 缺省 rollback 恢复
    cp = ws._abs(f"{pid}/bible/characters.json")
    cp.write_text(json.dumps([{"id": "char:evil", "name": "篡改者"}], ensure_ascii=False), encoding="utf-8")
    restored = restore_snapshot(ws, pid)  # 缺省 = latest = build-ok
    assert restored, "restore 应返回恢复文件列表"
    assert json.loads(cp.read_text(encoding="utf-8"))[0]["id"] == "char:protagonist"


def test_rollback_no_snapshot_raises(ws_factory):
    ws, pid = ws_factory("proj-f5l")
    _init_bp(ws, pid)  # 只建蓝图不构建 → 无快照
    import pytest

    with pytest.raises(FileNotFoundError):
        restore_snapshot(ws, pid)


# ---- --diff 影响分析 ----
def test_diff_affected_only_rebuilds_referencing_chapters(ws_factory):
    ws, pid = ws_factory("proj-f5m")
    _init_bp(ws, pid)
    _build_ok(ws, pid)
    # 改动 blueprint characters（bump rev）→ 影响引用 char:yelan 的章
    bp = Blueprint.load(ws, pid)
    for c in bp.section("characters"):
        if c.get("id") == "char:protagonist":
            c.setdefault("core_traits", []).append("新增特质")
    bp.bump_rev()
    bp.save(ws, pid)
    plan = diff_affected(ws, pid)
    assert plan.affected_chapters, plan.summary
    # 卷 1 两章都引用 char:yelan
    assert (1, 1) in plan.affected_chapters and (1, 2) in plan.affected_chapters
    assert not plan.rebuild_all
    # apply 删除细纲与节点 → build 重生成
    removed = plan.apply(ws, pid)
    assert removed >= 2
    assert not ws.outline_chapter_path(pid, 1, 1).exists()


def test_diff_rebuild_all_on_style_change(ws_factory):
    ws, pid = ws_factory("proj-f5n")
    _init_bp(ws, pid)
    _build_ok(ws, pid)
    bp = Blueprint.load(ws, pid)
    bp.set("style.narration", "华丽辞藻")
    bp.bump_rev()
    bp.save(ws, pid)
    plan = diff_affected(ws, pid)
    assert plan.rebuild_all and "style" in " ".join(plan.changed)


# ---- CLI：snapshots / rollback ----
def test_cli_snapshots_and_rollback(ws_factory, monkeypatch):
    ws, pid = ws_factory("proj-f5o")
    _init_bp(ws, pid)
    _build_ok(ws, pid)
    monkeypatch.chdir(str(ws._abs("")))
    runner = CliRunner()
    r = runner.invoke(cli, ["forge", "snapshots", pid])
    assert r.exit_code == 0, r.output
    assert "build" in r.output and "build-ok" in r.output, r.output  # 双快照都列出
    # 篡改后 rollback 命令回退（缺省 --to = 最近快照 build-ok）
    cp = ws._abs(f"{pid}/bible/style.json")
    cp.write_text(json.dumps({"narration": "被篡改"}, ensure_ascii=False), encoding="utf-8")
    r2 = runner.invoke(cli, ["forge", "rollback", pid])
    assert r2.exit_code == 0, r2.output
    assert "恢复" in r2.output
    style = json.loads(cp.read_text(encoding="utf-8"))
    assert style.get("narration") == "白描"
