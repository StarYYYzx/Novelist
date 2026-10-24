"""全书终润层测试（DL-015 / ADR-038 批次 3）。单测绝不真调 LLM——ScriptedProvider。
"""

from __future__ import annotations

import json

from novelist.core.polish_book import (
    _light_fact_check,
    _split_window_output,
    build_fingerprint,
    load_fingerprint,
    polish_book,
)
from novelist.providers.fake import ScriptedProvider
from novelist.storage.workspace import Workspace

CH1 = ("林澈站在山门前，掌心沁出细汗。三年了，他从外门杂役一路拼到今日。"
       "「今日大比，我不会再输。」他低声说，指尖摩挲着腰间的残缺玉佩。"
       "远处钟声响起，九名内门弟子依次登台。" * 6)
CH2 = ("苏晚抱着药箱穿过回廊，廊下弟子纷纷让路。她如今是丹房首席，"
       "再没人敢叫她「弃徒」。「让开。」她声音不高，却没人敢动。"
       "丹炉的火光映在她眼底，那里烧着一场持续十年的复仇。" * 6)


def _mk(tmp_path, pid="proj-polish", chapters=None):
    ws = Workspace(root=str(tmp_path))
    ws.create_project(pid)
    for (v, c), text in (chapters or {(1, 1): CH1, (1, 2): CH2}).items():
        ws.write_text(ws._abs(f"{pid}/chapters/{v}-{c}.md"), text)  # noqa: SLF001
    # 人物名清单来源（覆盖率复检消费）：bible 人物卡
    ws.write_json(ws._abs(f"{pid}/bible/characters.json"), [  # noqa: SLF001
        {"id": "char:lin", "name": "林澈"},
        {"id": "char:su", "name": "苏晚"},
    ])
    return ws, pid


def _fp_json():
    return json.dumps({
        "global": {"pov_tense": "第三人称限知/过去时",
                   "sentence_rhythm": "短句为主，动作段落节奏紧凑",
                   "taboos": ["仿佛在诉说", "空气仿佛凝固"],
                   "techniques": ["以器物细节代情绪"]},
        "voices": {"林澈": {"traits": "短句、克制、偶发自语", "sample": "「今日大比，我不会再输。」"},
                   "苏晚": {"traits": "命令式短句、冷淡", "sample": "「让开。」"}},
    }, ensure_ascii=False)


# ---------- 指纹 ----------

def test_build_fingerprint_windows_and_persist(tmp_path):
    ws, pid = _mk(tmp_path)
    fp = build_fingerprint(ws, pid, ScriptedProvider([{"final": _fp_json()}]))
    assert fp["global"]["pov_tense"].startswith("第三人称")
    assert "林澈" in fp["voices"] and "苏晚" in fp["voices"]
    # 持久化 + 可复用（polish 不再重建）
    assert load_fingerprint(ws, pid) == fp


# ---------- 窗口输出解析 ----------

def test_split_window_output_markers_and_fallback():
    body = ("=== 第1卷第1章 ===\n润色一\n\n=== 第1卷第2章 ===\n润色二")
    out = _split_window_output(body, [(1, 1), (1, 2)])
    assert out[(1, 1)] == "润色一" and out[(1, 2)] == "润色二"
    # 单章窗丢分隔符：整段正文按唯一键收下
    out2 = _split_window_output("一段超过三百字的润色正文" * 30, [(1, 1)])
    assert (1, 1) in out2 and len(out2[(1, 1)]) > 300


# ---------- 轻量事实复检 ----------

def test_light_fact_check_catches_overreach():
    rules = [{"card": "system-flow", "phase": "compile", "rule": "",
              "checks": [{"id": "sys_appear_min", "kind": "min_count",
                          "pattern": "【", "min": 1}]}]
    names = ["林澈", "苏晚"]
    # 正常句级润色（无系统流规则的场合）：过
    ok = _light_fact_check(CH1, CH1.replace("他低声说", "他轻声道"), [], names)
    assert ok == []
    # 系统流场合：原文过、润色稿把【】润没了 → 工艺规则违反
    good_sys = _light_fact_check("【任务】出发。林澈握紧玉佩。",
                                 "【任务】出发。林澈攥紧玉佩。", rules, names)
    assert good_sys == []
    bad1 = _light_fact_check("【任务】出发。", "出发。", rules, names)
    assert any("工艺规则违反" in p for p in bad1)
    # 越权 2：人物消失
    bad2 = _light_fact_check(CH1, CH1.replace("林澈", "少年"), rules, names)
    assert any("人物消失" in p for p in bad2)
    # 越权 3：篇幅异常（删掉一半内容）
    bad3 = _light_fact_check(CH1, CH1[: len(CH1) // 2], [], names)
    assert any("篇幅异常" in p for p in bad3)


# ---------- 全书润色主流程 ----------

def test_polish_book_writes_polished_and_report(tmp_path):
    ws, pid = _mk(tmp_path)
    polished1 = CH1.replace("他低声说", "他声音很轻") + "润色稿合格长度补充。" * 8
    polished2 = CH2.replace("她声音不高", "她语声平平") + "润色稿合格长度补充。" * 8
    win_out = (f"=== 第1卷第1章 ===\n{polished1}\n\n"
               f"=== 第1卷第2章 ===\n{polished2}")
    script = [
        {"final": _fp_json()},                    # 指纹窗口
        {"final": win_out},                       # 润色窗口
    ]
    report = polish_book(ws, pid, ScriptedProvider(script), window=4)
    assert report["polished"] == 2 and report["rolled_back"] == 0
    p1 = ws._abs(f"{pid}/chapters_polished/1-1.md").read_text(encoding="utf-8")  # noqa: SLF001
    assert "他声音很轻" in p1
    # 原文未动
    orig = ws._abs(f"{pid}/chapters/1-1.md").read_text(encoding="utf-8")  # noqa: SLF001
    assert "他低声说" in orig
    # 报告落盘
    rep = json.loads(ws._abs(f"{pid}/reports/polish_report.json")  # noqa: SLF001
                     .read_text(encoding="utf-8"))
    assert rep["polished"] == 2


def test_polish_book_rolls_back_overreach(tmp_path):
    """润色稿删人物（越权）→ 该章回滚原文，报告记录问题。"""
    ws, pid = _mk(tmp_path)
    bad = CH1.replace("林澈", "少年")  # 人物消失
    script = [
        {"final": _fp_json()},
        {"final": f"=== 第1卷第1章 ===\n{bad}\n\n=== 第1卷第2章 ===\n{CH2}"},
    ]
    report = polish_book(ws, pid, ScriptedProvider(script), window=4)
    assert report["rolled_back"] == 1 and report["polished"] == 1
    p1 = ws._abs(f"{pid}/chapters_polished/1-1.md").read_text(encoding="utf-8")  # noqa: SLF001
    assert "林澈" in p1  # 回滚 = 原文
    row = next(r for r in report["chapters"] if r["vol"] == 1 and r["ch"] == 1)
    assert any("人物消失" in p for p in row["problems"])
