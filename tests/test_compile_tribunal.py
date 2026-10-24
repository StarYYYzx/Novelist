"""编译期测试（DL-015 / ADR-038）：三视角判定器 + 证据堵口 + 约束闭包 + 欠账。

单测绝不真调 LLM（docs/09 §2.1）——ScriptedProvider / FakeProvider。
"""

from __future__ import annotations

import json

from novelist.consistency.tribunal import (
    Issue,
    aggregate,
    compile_pass,
    clear_debt_for_chapter,
    load_debt,
)
from novelist.providers.fake import ScriptedProvider
from novelist.storage.workspace import Workspace

CH = ("林澈盘膝而坐，丹田内灵气翻涌。三日前他还是炼气三层，如今突破在即。"
      "他握紧了那块残缺玉佩，这是母亲留下的唯一遗物。"
      "「明日宗门大比，我定要让所有人闭嘴。」他低声说。")


def _mk_project(tmp_path, pid="proj-compile") -> str:
    ws = Workspace(root=str(tmp_path))
    ws.create_project(pid)
    return pid


def _ws(tmp_path, pid="proj-compile") -> Workspace:
    return Workspace(root=str(tmp_path))


# ---------- 引用机器复核 + 严重度映射 ----------

def test_invalid_quote_dropped_and_severity_mapped():
    from novelist.consistency.tribunal import _parse_issues

    raw = json.dumps({"issues": [
        {"type": "境界倒退", "quote": "三日前他还是炼气三层", "basis": "worldstate 已是炼气七层"},
        {"type": "幻觉引用", "quote": "这段正文里根本不存在的话", "basis": "编造"},
    ]}, ensure_ascii=False)
    issues, invalid = _parse_issues(raw, "guardian", CH)
    assert invalid == 1 and len(issues) == 1
    assert issues[0].severity == "hard"          # 守护者 → hard（拍板映射）
    reader_issues, _ = _parse_issues(raw, "reader", CH)
    assert reader_issues[0].severity == "soft"   # 读者/编辑 → soft


# ---------- 多数决聚合 ----------

def test_aggregate_majority_rules():
    g = Issue("guardian", "境界倒退", "三日前他还是炼气三层", "worldstate 冲突", severity="hard")
    r = Issue("reader", "前后矛盾", "三日前他还是炼气三层", "前文已是七层", severity="soft")
    e = Issue("editor", "衔接缺失", "宗门大比怎么突然开始了", "细纲缺环", severity="soft")
    rep = aggregate([g, r, e])
    # g+r 同一问题（引用相同）→ 确认 hard；e 单视角 → 观察名单
    assert len(rep.hard) == 2 and not rep.soft
    assert len(rep.watchlist) == 1 and rep.watchlist[0].perspective == "editor"
    assert not rep.ok

    # 读者+编辑报同一问题（引用相同）→ soft（无守护者参与即非事实冲突）
    e2 = Issue("editor", "前后矛盾", "三日前他还是炼气三层", "细纲与正文冲突",
               severity="soft")
    rep2 = aggregate([r, e2])
    assert not rep2.hard and len(rep2.soft) == 2 and not rep2.watchlist


def test_aggregate_single_perspective_goes_watchlist():
    r = Issue("reader", "动机断裂", "他突然拔剑", "前文无铺垫", severity="soft")
    rep = aggregate([r])
    assert rep.ok and len(rep.watchlist) == 1


# ---------- 修复循环（compile_pass） ----------

def _json_step(perspective: str, issues: list[dict]) -> dict:
    return {"final": json.dumps({"issues": issues}, ensure_ascii=False)}


def test_compile_pass_rewrites_then_passes(tmp_path):
    """hard 确认 → 带证据重写 → 复检通过；欠账记 soft/观察名单。"""
    ws = _ws(tmp_path)
    pid = _mk_project(tmp_path)
    issue = {"type": "境界倒退", "quote": "三日前他还是炼气三层",
             "basis": "worldstate：林澈已是炼气七层", "suggestion": "改为回忆口吻"}
    clean = {"final": json.dumps({"issues": []}, ensure_ascii=False)}
    rewrite = {"final": CH.replace("三日前他还是炼气三层，如今突破在即",
                                   "回想起三日前还停留在炼气三层的自己，他嘴角微扬") + "修订扩充" * 200}
    script = [
        _json_step("guardian", [issue]),   # 第 1 轮 guardian：报 hard
        _json_step("reader", [issue]),     # 第 1 轮 reader：同问题 → 确认 hard
        clean,                             # 第 1 轮 editor：无问题
        rewrite,                           # 修复重写
        clean, clean, clean,               # 第 2 轮三视角：全清 → 通过
    ]
    text, report = compile_pass(ws, pid, 1, 1, CH, ScriptedProvider(script), rewrite_max=2)
    assert report["blocked"] is False
    assert report["rounds"] == 2
    assert "炼气三层" in text  # 修订稿被采纳（引用仍存在于原文窗口？——修订稿替换了该句）
    assert "修订扩充" in text or "回想起" in text
    debt = load_debt(ws, pid)
    assert debt == {} or not debt.get("by_volume")  # 全清后无 soft/观察欠账


def test_compile_pass_blocks_when_hard_unfixable(tmp_path):
    ws = _ws(tmp_path)
    pid = _mk_project(tmp_path)
    issue = {"type": "境界倒退", "quote": "三日前他还是炼气三层",
             "basis": "worldstate：已是七层", "suggestion": "改"}
    bad = _json_step("g", [issue])
    rewrite = {"final": CH + "修订" * 300}
    script = [bad, bad, bad, rewrite, bad, bad, bad]  # 两轮都报 → 阻断
    text, report = compile_pass(ws, pid, 1, 2, CH, ScriptedProvider(script), rewrite_max=1)
    assert report["blocked"] is True
    assert len(report["hard"]) >= 2
    # 阻断的 hard 也入欠账（blocked 标记），doctor 展示
    debt = load_debt(ws, pid)
    rows = debt["by_volume"]["1"]
    assert any(r.get("blocked") for r in rows)
    # 按章清欠
    removed = clear_debt_for_chapter(ws, pid, 1, 2)
    assert removed >= 1


def test_compile_pass_soft_goes_debt_not_block(tmp_path):
    """读者+编辑确认（无守护者）→ soft：不阻断、只入欠账。"""
    ws = _ws(tmp_path)
    pid = _mk_project(tmp_path)
    issue = {"type": "动机断裂", "quote": "他握紧了那块残缺玉佩",
             "basis": "前文未交代玉佩来历", "suggestion": "补一句"}
    soft_step = _json_step("r", [issue])
    # 视角固定按 guardian→reader→editor 消费脚本：守护者清白、读者+编辑报 → soft
    script = [{"final": json.dumps({"issues": []})}, soft_step, soft_step]
    text, report = compile_pass(ws, pid, 2, 3, CH, ScriptedProvider(script), rewrite_max=1)
    assert report["blocked"] is False
    assert len(report["soft"]) == 2 and not report["hard"]
    debt = load_debt(ws, pid)
    assert any(r["ch"] == 3 for r in debt["by_volume"]["2"])


# ---------- 证据堵口（DL-015） ----------

def test_chronicler_evidence_required_rejects_empty(tmp_path):
    from novelist.core.chronicler import Chronicler, Extraction, ExtractedEvent

    ws = _ws(tmp_path)
    pid = _mk_project(tmp_path)
    ch = Chronicler(ws, pid, llm=None, evidence_required=True)
    ex = Extraction(events=[
        ExtractedEvent(summary="林澈突破炼气四层", kind="turning_point"),   # 无证据
        ExtractedEvent(summary="林澈抚摸玉佩", kind="dialogue",
                       evidence="那块残缺玉佩"),                            # 证据合法
        ExtractedEvent(summary="幻觉事件", kind="turning_point",
                       evidence="正文里没有这句话"),                        # 证据不符
    ])
    ch._validate_evidence(ex, CH)  # noqa: SLF001 - 单测直查（类内协议）
    assert len(ex.events) == 1 and ex.events[0].summary == "林澈抚摸玉佩"
    assert len(ex.rejected) == 2
    assert any("无证据拒收" in r for r in ex.rejected)

    # 库默认（False）：无证据放行（旧 fixture 兼容），证据不符仍拒
    ch_lenient = Chronicler(ws, pid, llm=None)
    ex2 = Extraction(events=[
        ExtractedEvent(summary="林澈突破炼气四层", kind="turning_point"),
        ExtractedEvent(summary="幻觉事件", kind="turning_point", evidence="不存在的话"),
    ])
    ch_lenient._validate_evidence(ex2, CH)  # noqa: SLF001
    assert len(ex2.events) == 1 and len(ex2.rejected) == 1
