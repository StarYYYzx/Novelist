"""M3l F1 测试（docs/10 §13 / docs/08 F1）：seed + nodes + engine + AG1 端到端。

覆盖（**单测绝不真调 LLM**，docs/09 §2.1——全部用 FakeProvider/ScriptedProvider）：
- seed：提炼解析三形态 / 蓝图初始化与 provenance 分层 / 提炼失败兜底 / 非 TTY 降级
- nodes：节点协议解析 / 细纲 md 双通道（parse_gist + parse_key_events + parse_cast_decl）/
  book apply provenance 保护 / sync_bible 剥离与补默认 / worldstate 确定性合成
- engine：AG1 端到端（ScriptedProvider → bible + volumes + 卷 1 细纲 → produce_chapter
  bible_injected=True 且 cast 非空）/ resume 幂等 / 预算耗尽 / 解析失败回退父层
"""

from __future__ import annotations

import json

import pytest

from novelist.core.bible import parse_gist
from novelist.core.context import build_chapter_context
from novelist.core.orchestrator import parse_cast_decl, produce_chapter
from novelist.core.session import SessionInfo
from novelist.forge import Blueprint, run_seed
from novelist.forge.engine import build
from novelist.forge.nodes import (
    NodeContext,
    render_gist_md,
    run_node,
    sync_bible,
    synthesize_worldstate,
    _parse_node_reply,
)
from novelist.forge.seed import _parse_seed_spec
from novelist.providers.fake import FakeProvider, ScriptedProvider

SEED_META = {"title": "t", "genre": "修仙", "logline": "x",
             "scale": {"volumes": 1, "chapters_per_volume": 1, "target_words_per_chapter": 100}}

SEED_REPLY = json.dumps({
    "genre": "修仙", "template_suggestion": "修仙男频",
    "logline": "五五开系统，绑定他人共享修炼",
    "protagonist_hint": {"name": "叶蓝", "gender": "male", "cheat": "五五开系统"},
    "conflict": "废柴逆袭",
    "tone_hint": "热血激昂",
    "scale_hint": {"volumes": 2, "chapters_per_volume": 2},
    "time_origin": "叶蓝穿越之日",
    "unknowns": ["金手指规则细节", "结局走向"],
}, ensure_ascii=False)

BOOK_ARTIFACT = {
    "worldview": {"name": "落霞界", "power_system": {"mechanic": "五五开系统", "levels": ["练气", "筑基"]},
                  "rules": ["云纹令只认陆氏血脉"], "factions": ["落霞宗", "归墟会"]},
    "characters": [
        {"id": "char:yelan", "name": "叶蓝", "gender": "male", "role": "protagonist",
         "core_traits": ["谨慎", "重情"], "background": "穿越者",
         "power": {"level": "凡人", "faction": "落霞宗"}, "arc": "废柴到宗门救星",
         "first_appear": {"vol": 1, "ch": 1}},
        {"id": "char:yunxi", "name": "云曦", "gender": "female", "role": "mentor",
         "core_traits": ["飒爽", "护短"], "power": {"level": "筑基后期", "faction": "落霞宗"},
         "arc": "守护宗门", "first_appear": {"vol": 1, "ch": 2}},
    ],
    "locations": [{"id": "loc:luoxia", "name": "落霞宗", "category": "宗门", "desc": "主角所在宗门"}],
    "items": [{"id": "item:yunwen", "name": "云纹玉牌", "category": "信物", "desc": "母亲遗物"}],
    "style": {"tense": "过去", "narration": "白描为主", "glossary": [{"term": "五五开", "note": "绑定共享"}]},
    "threads": [{"id": "pt:yunwen", "desc": "云纹玉牌之谜", "scope": "volume", "target_vol": 1}],
    "volumes": [{"vol": 1, "title": "初入修行", "summary": "觉醒五五开", "key_beats": ["觉醒", "绑定"]},
                 {"vol": 2, "title": "宗门风波", "summary": "宗门大比", "key_beats": ["大比"]}],
    "time_origin": "叶蓝穿越之日",
}


def _node_reply(artifact: dict, decide: str = "done") -> str:
    return json.dumps({"artifact": artifact, "decide": decide,
                       "reason": "测试脚本固定回复"}, ensure_ascii=False)


def _volume_artifact(vol: int) -> dict:
    return {"vol": vol, "title": f"第 {vol} 卷", "summary": f"第 {vol} 卷主线",
            "key_beats": ["转折A"], "threads_to_payoff": [], "chapter_notes": "前段铺垫，末段冲突"}


def _chapter_artifact(ch: int) -> dict:
    return {"title": f"章 {ch}", "pov": "第三人称限知（叶蓝视角）",
            "key_events": [f"事件{ch}-1", f"事件{ch}-2"], "turns": [f"转折{ch}"],
            "characters": ["char:yelan"], "threads_involved": ["pt:yunwen"], "after_days": 0}


def _ag1_script(volumes: int = 2, chapters: int = 2) -> list[dict]:
    """按引擎真实调用顺序排脚本：seed → book → v1 → v1 的 chapters → v2…（卷闸门）。"""
    script = [{"final": SEED_REPLY}, {"final": _node_reply(BOOK_ARTIFACT)}]
    for v in range(1, volumes + 1):
        script.append({"final": _node_reply(_volume_artifact(v))})
        if v == 1:
            for c in range(1, chapters + 1):
                script.append({"final": _node_reply(_chapter_artifact(c))})
    return script


def _build_script(volumes: int = 2, chapters: int = 2) -> list[dict]:
    """build-only 脚本（蓝图已建，无 seed 提炼项）：book → v1 → v1 的 chapters → v2…"""
    script = [{"final": _node_reply(BOOK_ARTIFACT)}]
    for v in range(1, volumes + 1):
        script.append({"final": _node_reply(_volume_artifact(v))})
        if v == 1:
            for c in range(1, chapters + 1):
                script.append({"final": _node_reply(_chapter_artifact(c))})
    return script


# ---- seed：提炼解析 ----
def test_seed_spec_parse_plain_json():
    spec = _parse_seed_spec(SEED_REPLY)
    assert spec.genre == "修仙"
    assert spec.protagonist_hint["name"] == "叶蓝"
    assert spec.protagonist_hint["gender"] == "male"
    assert spec.scale_hint == {"volumes": 2, "chapters_per_volume": 2}
    assert spec.time_origin == "叶蓝穿越之日"
    assert len(spec.unknowns) == 2


def test_seed_spec_parse_fenced_json():
    spec = _parse_seed_spec(f"```json\n{SEED_REPLY}\n```")
    assert spec.logline == "五五开系统，绑定他人共享修炼"


def test_seed_spec_parse_rejects_non_json():
    with pytest.raises(ValueError):
        _parse_seed_spec("抱歉，我无法生成 JSON。")


def test_seed_spec_parse_tolerates_prose_wrapper():
    spec = _parse_seed_spec(f"好的，这是提炼结果：\n{SEED_REPLY}\n（以上）")
    assert spec.genre == "修仙"


# ---- seed：run_seed（smoke，不构建）----
def test_run_seed_smoke_builds_blueprint(ws_factory):
    ws, pid = ws_factory("proj-f1s")
    provider = FakeProvider(reply=SEED_REPLY)

    # 直接验证 seed 的提炼+蓝图部分（build 拆开测）
    from novelist.forge.seed import _init_blueprint
    from novelist.forge.genres import load_pack_for

    spec = _parse_seed_spec(SEED_REPLY)
    pack = load_pack_for("修仙男频")
    bp = _init_blueprint(ws, pid, "一句话创意", spec, pack, "修仙男频", None, None, None)
    bp.save(ws, pid)
    assert bp.get("meta.title")
    assert bp.get("meta.scale") == {"volumes": 2, "chapters_per_volume": 2,
                                    "target_words_per_chapter": 2400}
    # provenance 分层：提炼 llm、包默认 template
    assert bp.get_provenance("meta.logline")["src"] == "llm"
    assert bp.get_provenance("meta.template")["src"] == "template"
    assert bp.get_provenance("worldview.power_system.levels")["src"] == "template"
    # 主角骨架建档
    proto = next(c for c in bp.section("characters") if c.get("role") == "protagonist")
    assert proto["name"] == "叶蓝" and proto["gender"] == "male"
    # 提炼的机制覆盖模板
    assert bp.get("worldview.power_system.mechanic") == "五五开系统"


def test_run_seed_scales_user_cli_overrides(ws_factory):
    ws, pid = ws_factory("proj-f1u")
    from novelist.forge.seed import _init_blueprint
    from novelist.forge.genres import load_pack_for

    spec = _parse_seed_spec(SEED_REPLY)
    bp = _init_blueprint(ws, pid, "brief", spec, load_pack_for("修仙男频"), "修仙男频",
                         3, 20, 3000)
    # CLI 显式参数 → user provenance（受保护）
    assert bp.get("meta.scale") == {"volumes": 3, "chapters_per_volume": 20,
                                    "target_words_per_chapter": 3000}
    assert bp.get_provenance("meta.scale")["src"] == "user"


def test_run_seed_fallback_on_bad_reply(ws_factory, capsys):
    ws, pid = ws_factory("proj-f1f")
    res = run_seed(ws, pid, "一句话", provider=FakeProvider(reply="not json at all"),
                   volumes=1, chapters_per_volume=1, target_words=500, max_calls=60)
    # 提炼失败 → 兜底 spec（不崩）；构建随 book 节点失败而回退（无细纲）
    assert any("提炼失败" in w for w in res.warnings)
    assert res.spec.logline == "一句话"  # 兜底用 brief
    assert res.build.get("ok") is False
    capsys.readouterr()


def test_run_seed_interactive_downgrades_on_no_tty(ws_factory, monkeypatch, capsys):
    ws, pid = ws_factory("proj-f1i")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    res = run_seed(ws, pid, "一句话", provider=FakeProvider(reply=SEED_REPLY),
                   mode="interactive", volumes=1, chapters_per_volume=1, target_words=500)
    assert res.mode_used == "auto"
    assert any("非交互环境" in w for w in res.warnings)
    # 降级留痕
    from novelist.forge import read_transcript

    events = read_transcript(ws, pid)
    assert any(e["event"] == "seed.downgrade" for e in events)
    capsys.readouterr()


# ---- nodes：节点协议与落盘 ----
def test_parse_node_reply_protocol():
    node = _parse_node_reply('{"artifact": {"a": 1}, "decide": "expand", "reason": "r", "children": [{"id": "x"}]}')
    assert node["artifact"] == {"a": 1}
    assert node["decide"] == "expand"
    with pytest.raises(ValueError):
        _parse_node_reply("no json here")
    # 非法 decide 兜底 done
    node = _parse_node_reply('{"artifact": null, "decide": "huh", "reason": "r"}')
    assert node["decide"] == "done"


def test_render_gist_md_dual_channel(ws_factory):
    ws, pid = ws_factory("proj-f1g")
    gist = {"vol": 1, "ch": 3, "title": "觉醒", "pov": "第三人称限知（叶蓝视角）",
            "key_events": ["系统觉醒", "绑定云曦"], "turns": ["转折"],
            "characters": ["char:yelan"], "threads_involved": ["pt:yunwen"], "after_days": 2}
    md = render_gist_md(gist, 1, 3, ["叶蓝"])
    ws.write_text(ws.outline_chapter_path(pid, 1, 3), md)
    # 三通道全通：front-matter 结构化 / 行内 key_events / 行内出场人物
    from novelist.core.context import parse_key_events

    g = parse_gist(ws, pid, 1, 3)
    assert g["title"] == "觉醒"
    assert g["key_events"] == ["系统觉醒", "绑定云曦"]
    assert g["after_days"] == 2
    assert parse_cast_decl(md) == ["叶蓝"]
    # orchestrator 消费路径（行内 regex，带 JSON 引号也可解析）
    assert parse_key_events(md) == ["系统觉醒", "绑定云曦"]


def test_book_apply_protects_user_fields(ws_factory):
    ws, pid = ws_factory("proj-f1p")
    bp = Blueprint.blank(dict(SEED_META))
    bp.data["meta"]["scale"] = {"volumes": 2, "chapters_per_volume": 2, "target_words_per_chapter": 100}
    bp.data["meta"]["title"] = "用户书名"
    bp.set_provenance("meta.title", "user", 1.0)
    bp.data["worldview"] = {"power_system": {"mechanic": "用户钦定机制"}}
    bp.set_provenance("worldview.power_system.mechanic", "user", 1.0)
    bp.save(ws, pid)

    ctx = NodeContext(ws=ws, project_id=pid, bp=bp, provider=FakeProvider(reply="x"),
                      pack={}, spec=None)
    from novelist.forge.nodes import _apply_book

    node = {"artifact": {**BOOK_ARTIFACT, "worldview": {
        **BOOK_ARTIFACT["worldview"],
        "power_system": {"mechanic": "模型想改", "levels": ["练气", "筑基"]}}},
            "decide": "done", "reason": "r"}
    _apply_book(ctx, node)
    # user 保护字段不被覆盖；非保护字段被模型填充
    assert bp.get("worldview.power_system.mechanic") == "用户钦定机制"
    assert bp.get("worldview.name") == "落霞界"
    assert bp.get("meta.title") == "用户书名"
    # 主角按模型卡 upsert
    assert bp.find_by_id("characters", "char:yelan")["name"] == "叶蓝"


def test_sync_bible_strips_role_and_fills_defaults(ws_factory):
    ws, pid = ws_factory("proj-f1b")
    bp = Blueprint.blank(dict(SEED_META))
    bp.data["meta"]["scale"] = {"volumes": 1, "chapters_per_volume": 1, "target_words_per_chapter": 100}
    bp.upsert("characters", {"id": "char:yelan", "name": "叶蓝", "gender": "male",
                             "role": "protagonist", "core_traits": []})
    bp.upsert("threads", {"id": "pt:x", "desc": "伏笔"})
    bp.data["worldview"] = {"name": "落霞界", "power_system": {"levels": ["练气"]}}
    written = sync_bible(ws, pid, bp)
    assert "bible/characters.json" in written
    chars = json.loads(ws._abs(f"{pid}/bible/characters.json").read_text(encoding="utf-8"))  # noqa: SLF001
    assert chars[0]["is_protagonist"] is True
    assert "role" not in chars[0]  # 落盘剥离
    assert chars[0]["status"] == "active"
    # style 补 protagonist 引用
    st = json.loads(ws._abs(f"{pid}/bible/style.json").read_text(encoding="utf-8"))  # noqa: SLF001
    assert st["protagonist"]["id"] == "char:yelan"
    # worldview 补 id
    wv = json.loads(ws._abs(f"{pid}/bible/worldview.json").read_text(encoding="utf-8"))  # noqa: SLF001
    assert wv["id"] == "world:main"


def test_synthesize_worldstate_deterministic(ws_factory):
    bp = Blueprint.blank(dict(SEED_META))
    bp.data["meta"]["time_origin"] = "叶蓝穿越之日"
    bp.data["meta"]["scale"] = {"volumes": 1, "chapters_per_volume": 1, "target_words_per_chapter": 100}
    bp.upsert("characters", {"id": "char:yelan", "name": "叶蓝", "role": "protagonist",
                             "power": {"level": "凡人", "faction": "落霞宗"}})
    ws_state = synthesize_worldstate(bp)
    assert ws_state["time"] == {"now": 0, "origin_text": "叶蓝穿越之日"}
    assert ws_state["pending"] == []
    c = ws_state["characters"]["char:yelan"]
    assert c["name"] == "叶蓝" and c["realm"] == "凡人"
    assert c["history"] == [] and c["dead"] is False


# ---- engine：AG1 端到端 ----
def test_ag1_seed_to_chapter_production(ws_factory):
    """AG1：一句话 + ScriptedProvider → 项目可直接 chapter 1 1 且 bible_injected=True、cast 非空。"""
    ws, pid = ws_factory("proj-ag1")
    provider = ScriptedProvider(_ag1_script(volumes=2, chapters=2))
    res = run_seed(ws, pid, "五五开系统修仙文", provider=provider,
                   volumes=2, chapters_per_volume=2, target_words=1000)
    assert res.ok, res.warnings
    b = res.build
    assert b["volumes_written"] == 2 and b["chapters_written"] == 2

    # 落盘断言
    vols = json.loads(ws.outline_volumes_path(pid).read_text(encoding="utf-8"))
    assert [v["vol"] for v in vols] == [1, 2]
    assert vols[0]["chapter_range"] == [1, 2]
    gist = parse_gist(ws, pid, 1, 1)
    assert gist and gist["key_events"] == ["事件1-1", "事件1-2"]
    assert parse_cast_decl(ws.outline_chapter_path(pid, 1, 1).read_text(encoding="utf-8")) == ["叶蓝"]
    # bible 完整
    chars = json.loads(ws.bible_path(pid, "characters").read_text(encoding="utf-8"))
    assert any(c.get("is_protagonist") for c in chars)
    wv = json.loads(ws.bible_path(pid, "worldview").read_text(encoding="utf-8"))
    assert wv["name"] == "落霞界"
    wst = json.loads(ws.bible_path(pid, "worldstate").read_text(encoding="utf-8"))
    assert wst["time"]["now"] == 0

    # AG1 判据：chapter 1 1 直出且 bible_injected=True
    from novelist.core.approval import ApprovalQueue
    from novelist.core.tools import PermissionGate
    from novelist.tools import build_registry

    gate = PermissionGate()
    approvals = ApprovalQueue()
    reg = build_registry(ws, gate=gate, approvals=approvals, decision_fn=lambda r: "allow")
    ctx = build_chapter_context(ws, pid, 1, 1)
    assert ctx.cast, "cast 必须非空（AG1）"
    assert any("叶蓝" in (c.get("name") or "") for c in ctx.cast)

    prod = produce_chapter(ws, pid, 1, 1,
                           FakeProvider(reply="第一章正文。叶蓝觉醒了五五开系统，绑定云曦共享修炼，"
                                               "境界悄然突破。"),
                           session=SessionInfo(project_id=pid, agent="orchestrator"),
                           registry=reg, prefer_direct=True, generation_tokens=2000,
                           inject_bible=True, jit_characters=False)
    assert prod.ok, prod.result
    assert prod.bible_injected is True, "AG1：bible 必须注入"


def test_engine_resume_is_idempotent(ws_factory, capsys):
    ws, pid = ws_factory("proj-f1r")
    # 先建蓝图（直接走 run_seed 会重建；这里手动初始化后 build 两次）
    from novelist.forge.seed import _init_blueprint
    from novelist.forge.genres import load_pack_for

    spec = _parse_seed_spec(SEED_REPLY)
    bp = _init_blueprint(ws, pid, "brief", spec, load_pack_for("修仙男频"), "修仙男频",
                         2, 2, 1000)
    bp.save(ws, pid)
    r1 = build(ws, pid, provider=ScriptedProvider(_build_script(volumes=2, chapters=2)),
               max_calls=60)
    assert r1.ok and r1.chapters_written == 2
    n1 = r1.calls_used

    # resume：全部节点已落盘 → 不再调用 LLM，calls_used 不增长
    r2 = build(ws, pid, provider=ScriptedProvider([]), max_calls=60, resume=True)
    assert r2.calls_used == n1  # 续跑零新调用
    assert r2.chapters_written == 0
    # 落盘内容未被破坏
    assert parse_gist(ws, pid, 1, 2)["key_events"] == ["事件2-1", "事件2-2"]
    capsys.readouterr()


def test_engine_budget_exhaustion(ws_factory, capsys):
    ws, pid = ws_factory("proj-f1e")
    from novelist.forge.seed import _init_blueprint
    from novelist.forge.genres import load_pack_for

    spec = _parse_seed_spec(SEED_REPLY)
    bp = _init_blueprint(ws, pid, "brief", spec, load_pack_for("修仙男频"), "修仙男频",
                         2, 2, 1000)
    bp.save(ws, pid)
    r = build(ws, pid, provider=ScriptedProvider(_build_script(volumes=2, chapters=2)),
              max_calls=1)
    assert r.budget_exhausted is True
    assert any("预算耗尽" in w for w in r.warnings)
    assert r.calls_used <= 2  # 1 次调用 + 可能 1 次重试
    capsys.readouterr()


def test_engine_parse_failure_falls_back_to_parent(ws_factory, capsys):
    """解析失败重试一次仍失败 → 回退父层产物（book 失败用 seed 骨架，chapter 失败不落盘）。"""
    ws, pid = ws_factory("proj-f1x")
    from novelist.forge.seed import _init_blueprint
    from novelist.forge.genres import load_pack_for

    spec = _parse_seed_spec(SEED_REPLY)
    bp = _init_blueprint(ws, pid, "brief", spec, load_pack_for("修仙男频"), "修仙男频",
                         1, 2, 1000)
    bp.save(ws, pid)
    # book/volume 失败（垃圾回复）→ 用 seed 骨架继续；chapter 失败 → 不落盘
    r = build(ws, pid, provider=FakeProvider(reply="不是 JSON 的回复文本"),
              max_calls=60)
    assert any("回退父层产物" in w or "重试" in w for w in r.warnings)
    # book 失败但 seed 骨架仍在（worldview/主角不丢）
    assert json.loads(ws.bible_path(pid, "worldview").read_text(encoding="utf-8")).get("id")
    # 细纲文件不落盘（解析失败）
    assert not ws.outline_chapter_path(pid, 1, 1).exists()
    capsys.readouterr()


def test_cli_forge_seed_smoke(ws_factory, monkeypatch):
    """CLI 集成：forge seed --smoke 只提炼 + 建蓝图，不构建。"""
    from click.testing import CliRunner
    from novelist.cli import cli

    ws, pid = ws_factory("proj-f1c")
    runner = CliRunner()
    monkeypatch.chdir(str(ws._abs("")))  # noqa: SLF001 - workspace 根 = CWD（config 缺省）
    result = runner.invoke(cli, ["forge", "seed", "一句话", "--provider", "fake",
                                 "--smoke", "--volumes", "1",
                                 "--chapters-per-volume", "1", "--max-calls", "60"])
    assert result.exit_code == 0, result.output
    # 蓝图已建、正文未建（smoke 不构建）
    assert (ws._abs(f"{pid}/workspace/forge/blueprint.json")).exists()  # noqa: SLF001
    assert not ws.outline_chapter_path(pid, 1, 1).exists()
