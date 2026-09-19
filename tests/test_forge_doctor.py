"""蓝图体检（forge/doctor，2026-09-19 档 1）测试。

单测绝不真调 LLM（docs/09 §2.1）——FakeProvider/ScriptedProvider/异常桩。
"""

from __future__ import annotations

import json

from novelist.forge.doctor import DOCTOR_REL, precheck, run_doctor
from novelist.forge.state import Blueprint
from novelist.providers.fake import FakeProvider, ScriptedProvider


def _bp_with(ws, pid, *, characters=None, threads=None, scale=None, volumes=None):
    bp = Blueprint.blank()
    bp.data["meta"] = {"title": "测试书", "genre": "修仙", "logline": "一句话卖点",
                       "scale": scale or {"volumes": 1, "chapters_per_volume": 2,
                                          "target_words_per_chapter": 100}}
    bp.data["characters"] = characters or []
    bp.data["threads"] = threads or []
    bp.data["volumes"] = volumes or []
    bp.save(ws, pid)
    return bp


def test_precheck_clean_blueprint_quiet(ws_factory):
    ws, pid = ws_factory("proj-doc-clean")
    bp = _bp_with(ws, pid, characters=[{"id": "char:a", "name": "甲",
                                        "power": {"level": "练气"}}],
                  threads=[{"id": "pt:x", "desc": "有事", "name": "某条线",
                            "status": "planted"}])
    assert precheck(bp) == []


def test_precheck_duplicate_and_generic_protagonist(ws_factory):
    """真机形态（2026-09-19 上午）：泛型卡 char:protagonist 与实名卡并存。"""
    ws, pid = ws_factory("proj-doc-dup")
    bp = _bp_with(ws, pid, characters=[
        {"id": "char:protagonist", "name": "林原"},
        {"id": "char:linyuan", "name": "林原", "power": {"level": "练气前期"}},
    ])
    issues = [f["issue"] for f in precheck(bp)]
    assert any("2 张卡并存" in i for i in issues)
    assert any("泛型主角卡" in i for i in issues)
    assert any(f["level"] == "block" for f in precheck(bp))


def test_precheck_threads_and_scale(ws_factory):
    ws, pid = ws_factory("proj-doc-th")
    bp = _bp_with(ws, pid,
                  # 真机形态：desc 有字但 name 空（schema 不允许空 desc）
                  threads=[{"id": f"pt:t{i}", "desc": f"伏笔{i}", "name": "",
                            "status": "unplanned"} for i in range(15)],
                  scale={"volumes": 6, "chapters_per_volume": 100,
                         "target_words_per_chapter": 2400})
    issues = [f["issue"] for f in precheck(bp)]
    assert any("缺描述或名字" in i for i in issues)
    assert any("全部 unplanned" in i for i in issues)
    assert any("600 章" in i for i in issues)


def test_precheck_volume_arc_gap(ws_factory):
    ws, pid = ws_factory("proj-doc-arc")
    bp = _bp_with(ws, pid, volumes=[{"vol": 1, "title": "卷一",
                                     "chapter_range": [1, 2], "arc": {"goal": "x"}}])
    assert any("arc 缺 goal/outcome" in f["issue"] for f in precheck(bp))


def test_run_doctor_without_provider_precheck_only(ws_factory, tmp_path):
    """provider=None：只跑确定性预检，报告照常落盘。"""
    ws, pid = ws_factory("proj-doc-nollm")
    bp = _bp_with(ws, pid, characters=[{"id": "char:a", "name": "甲"}])  # 缺境界 → 1 条预检
    rep = run_doctor(ws, pid, bp, None, log=lambda _t: None)
    assert rep is not None
    assert len(rep["precheck"]) == 1 and rep["findings"] == []
    md = ws._abs(f"{pid}/{DOCTOR_REL}")  # noqa: SLF001
    assert md.exists() and "确定性预检" in md.read_text(encoding="utf-8")


def test_run_doctor_agent_findings_parsed(ws_factory):
    """LLM 证据环返回 JSON findings → 解析入报告（FakeProvider 无 tool_calling → 单轮直出）。"""
    ws, pid = ws_factory("proj-doc-llm")
    bp = _bp_with(ws, pid)
    reply = {"final": json.dumps({"findings": [
        {"level": "block", "module": "worldview",
         "issue": "扬名继承被写成世界铁律，但它是主角独有系统",
         "advice": "移到主角卡 cheat 字段"}]}, ensure_ascii=False)}
    rep = run_doctor(ws, pid, bp, ScriptedProvider([reply]), log=lambda _t: None)
    assert rep["findings"] and rep["findings"][0]["module"] == "worldview"
    assert "扬名" in rep["findings"][0]["issue"]


def test_run_doctor_non_json_reply_degrades_empty(ws_factory):
    """agent 返回非 JSON（FakeProvider 默认文本）→ findings 空，不炸。"""
    ws, pid = ws_factory("proj-doc-txt")
    bp = _bp_with(ws, pid)
    rep = run_doctor(ws, pid, bp, FakeProvider(), log=lambda _t: None)
    assert rep is not None and rep["findings"] == []


def test_run_doctor_llm_failure_still_reports_precheck(ws_factory):
    """LLM 侧抛异常 → 软失败：报告仍落盘，预检结果保留（绝不阻断构建）。"""
    ws, pid = ws_factory("proj-doc-boom")
    bp = _bp_with(ws, pid, characters=[{"id": "char:a", "name": "甲"}])

    class _Boom:
        capabilities = type("C", (), {"tool_calling": False})()

        def complete(self, req):
            raise RuntimeError("provider 炸了")

    rep = run_doctor(ws, pid, bp, _Boom(), log=lambda _t: None)
    assert rep is not None
    assert len(rep["precheck"]) == 1 and rep["findings"] == []
    assert ws._abs(f"{pid}/{DOCTOR_REL}").exists()  # noqa: SLF001


def test_build_runs_doctor_and_writes_report(ws_factory):
    """端到端：build 末尾自动跑体检，doctor.md 落盘；transcript 有 doctor.done。"""
    from test_m13_forge_f1 import SEED_REPLY, _build_script
    from novelist.forge.engine import build
    from novelist.forge.seed import _init_blueprint, _parse_seed_spec
    from novelist.forge.genres import load_pack_for

    ws, pid = ws_factory("proj-doc-e2e")
    spec = _parse_seed_spec(SEED_REPLY)
    bp = _init_blueprint(ws, pid, "brief", spec, load_pack_for("修仙男频"), "修仙男频",
                         2, 2, 1000)
    bp.save(ws, pid)
    # 体检 agent 也吃同一个 ScriptedProvider：构建脚本之外补一条 JSON 答复
    script = _build_script(volumes=2, chapters=2) + [
        {"final": json.dumps({"findings": [{"level": "warn", "module": "meta",
                                            "issue": "体检发现", "advice": "看着办"}]},
                             ensure_ascii=False)}]
    res = build(ws, pid, provider=ScriptedProvider(script), max_calls=60,
                deepen=False, gate=False)
    assert res.ok
    md = ws._abs(f"{pid}/{DOCTOR_REL}")  # noqa: SLF001
    assert md.exists()
    import json as _j

    events = [_j.loads(x) for x in
              ws._abs(f"{pid}/workspace/forge/transcript.jsonl")  # noqa: SLF001
              .read_text(encoding="utf-8").splitlines() if x.strip()]
    assert any(e.get("event") == "doctor.done" for e in events)


def test_build_no_doctor_skips(ws_factory):
    """doctor=False：不写报告、不消耗 provider 调用。"""
    from test_m13_forge_f1 import SEED_REPLY, _build_script
    from novelist.forge.engine import build
    from novelist.forge.seed import _init_blueprint, _parse_seed_spec
    from novelist.forge.genres import load_pack_for

    ws, pid = ws_factory("proj-doc-off")
    spec = _parse_seed_spec(SEED_REPLY)
    bp = _init_blueprint(ws, pid, "brief", spec, load_pack_for("修仙男频"), "修仙男频",
                         2, 2, 1000)
    bp.save(ws, pid)
    script = _build_script(volumes=2, chapters=2)
    res = build(ws, pid, provider=ScriptedProvider(script), max_calls=60,
                deepen=False, gate=False, doctor=False)
    assert res.ok
    assert not ws._abs(f"{pid}/{DOCTOR_REL}").exists()  # noqa: SLF001
