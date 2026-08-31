"""分阶段工作流测试（第九批·用户设想，2026-08-31 拍板）。

覆盖：
- VolumeContext：chapter_range 解析 / 卷内坐标 / 缺失卷的保守默认
- PhasePolicy：三阶段判定矩阵（tail 优先 > opening > writing）与参数覆盖
- established_ratio：实体池填充率
- payoff_checklist / payoff_prompt_lines：卷级回收清单（scope/target_vol 容错）
- EntityTracker：开篇配额推迟（deferred 持久化 + 出队）与旧格式兼容
- Chronicler：收尾期 paid_off 自动判定
- R-THREAD：收尾期 warn / 卷末 block
"""

from __future__ import annotations

import json

from novelist.consistency.rules import run_rule_checks
from novelist.core.chronicler import Chronicler, ExtractedEvent
from novelist.core.entity import EntityTracker
from novelist.core.phase import (Phase, PhasePolicy, VolumeContext,
                                 established_ratio, payoff_checklist,
                                 payoff_prompt_lines)
from novelist.storage.checkpoint import Checkpoint
from novelist.storage.workspace import Workspace


def _project(tmp_path) -> tuple[Workspace, str]:
    ws = Workspace(root=str(tmp_path))
    pid = "proj-t"
    ws.create_project(pid)
    Checkpoint(ws).save(pid, {"id": pid, "pipeline_state": "正文"})
    return ws, pid


def _write(ws, pid, rel, data):
    ws.write_json(ws._abs(f"{pid}/{rel}"), data)


def _seed_volume(ws, pid, end=20):
    _write(ws, pid, "outline/volumes.json", [
        {"id": "vol:1", "vol": 1, "order": 1, "title": "五五开",
         "summary": "第一卷主线", "chapter_range": [1, end], "target_words": 48000,
         "threads_to_payoff": ["thread:yubi"]}])
    _write(ws, pid, "bible/plot_threads.json", [
        {"id": "thread:yubi", "status": "active", "desc": "断玉佩的来历与封印",
         "scope": "volume", "target_vol": 1},
        {"id": "thread:mozun", "status": "active", "desc": "魔尊残魂注视人间",
         "scope": "book"},
        {"id": "thread:xieyi", "status": "active", "desc": "与云清瑶的约定",
         "scope": "volume", "target_vol": 2},
    ])


# ---------------------------------------------------------------- VolumeContext

def test_volume_context_parse(tmp_path):
    ws, pid = _project(tmp_path)
    _seed_volume(ws, pid, end=20)
    v = VolumeContext.load(ws, pid, 1)
    assert (v.start, v.end, v.order) == (1, 20, 1)
    assert v.ch_in_volume(5) == 5
    assert v.chapters_left(19) == 2
    assert v.chapters_left(20) == 1
    assert v.total_chapters() == 20
    assert v.threads_to_payoff == ["thread:yubi"]


def test_volume_context_missing_is_conservative(tmp_path):
    """无 volumes.json / 无该卷：end=0 → 永不判收尾（保守）。"""
    ws, pid = _project(tmp_path)
    v = VolumeContext.load(ws, pid, 3)
    assert v.end == 0
    assert v.chapters_left(1) >= 10 ** 6
    phase, _ = PhasePolicy().judge(v, 1)
    assert phase is not Phase.TAIL


# ---------------------------------------------------------------- PhasePolicy

def test_judge_matrix(tmp_path):
    v = VolumeContext(vol=1, start=1, end=20)
    pol = PhasePolicy()  # opening=2 tail=2 ratio=0.6

    # 收尾期优先（硬时间约束），即便 established 占比低
    p1, r1 = pol.judge(v, 19)
    p2, r2 = pol.judge(v, 20)
    assert (p1, p2) == (Phase.TAIL, Phase.TAIL)
    assert "tail_chapters" in r2

    # 开篇：卷内章号
    p3, r3 = pol.judge(v, 2)
    assert p3 is Phase.OPENING and "opening_chapters" in r3
    # 开篇：填充率兜底（第 5 章，占比 0.5 < 0.6）
    p4, _ = pol.judge(v, 5, established_ratio=0.5)
    assert p4 is Phase.OPENING
    # 行文
    p5, _ = pol.judge(v, 5, established_ratio=0.8)
    assert p5 is Phase.WRITING


def test_policy_config_override(tmp_path):
    ws, pid = _project(tmp_path)
    _seed_volume(ws, pid)
    _write(ws, pid, "bible/worldview.json",
           {"phase_policy": {"opening_chapters": 4, "tail_chapters": 5}})
    pol = PhasePolicy.load(ws, pid)
    assert pol.opening_chapters == 4 and pol.tail_chapters == 5
    v = VolumeContext.load(ws, pid, 1)
    assert pol.judge(v, 16)[0] is Phase.TAIL


def test_generation_tokens_factor():
    pol = PhasePolicy(opening_length_factor=1.3)
    assert pol.generation_tokens(1000, Phase.OPENING) == 1300
    assert pol.generation_tokens(1000, Phase.WRITING) == 1000


# ---------------------------------------------------------------- 填充率

def test_established_ratio(tmp_path):
    ws, pid = _project(tmp_path)
    _write(ws, pid, "bible/characters.json", [
        {"id": "char:a", "name": "甲"}, {"id": "char:b", "name": "乙"}])
    _write(ws, pid, "bible/settings.json", [{"id": "set:s", "term": "灵根"}])
    t = EntityTracker.load(ws, pid)
    assert established_ratio(t) == 0.0
    t.entities["char:a"].stage = "established"
    assert established_ratio(t) == pytest_approx(1 / 3)
    assert established_ratio(None) is None


def pytest_approx(x):
    from pytest import approx
    return approx(x)


# ---------------------------------------------------------------- 回收清单

def test_payoff_checklist_scope(tmp_path):
    """volume 线进清单（target_vol 匹配）；book 线不进；他卷线不进。"""
    ws, pid = _project(tmp_path)
    _seed_volume(ws, pid)
    checklist = payoff_checklist(ws, pid, 1, 19)
    ids = [t["id"] for t in checklist["threads"]]
    assert ids == ["thread:yubi"]  # mozun 是 book 线、xieyi 在第 2 卷


def test_payoff_checklist_legacy_defaults(tmp_path):
    """旧数据（无 scope/target_vol）：缺省按 volume 处理，target_vol 取 planted.vol。"""
    ws, pid = _project(tmp_path)
    _write(ws, pid, "bible/plot_threads.json", [
        {"id": "thread:old", "status": "active", "desc": "老格式线",
         "planted": {"vol": 1, "ch": 3}}])
    ids = [t["id"] for t in payoff_checklist(ws, pid, 1, 19)["threads"]]
    assert ids == ["thread:old"]


def test_payoff_prompt_lines(tmp_path):
    checklist = {"threads": [{"id": "thread:yubi", "desc": "断玉佩来历", "status": "active"}],
                 "dormant": ["沈青梧（8 章后未出场）"]}
    lines = payoff_prompt_lines(checklist, is_final=True)
    text = "\n".join(lines)
    assert "必须给出明确交代" in text and "thread:yubi" in text and "沈青梧" in text
    assert "不得再开新钩子" in text


# ---------------------------------------------------------------- EntityTracker 推迟

def test_budget_defer_roundtrip(tmp_path):
    ws, pid = _project(tmp_path)
    _write(ws, pid, "bible/characters.json", [
        {"id": f"char:{i}", "name": n} for i, n in enumerate(["甲", "乙", "丙", "丁", "戊"])])
    t = EntityTracker.load(ws, pid)
    alerts = t.budget_check(["char:0", "char:1", "char:2", "char:3", "char:4"],
                            quota=3, defer_over=True)
    assert alerts and len(t.deferred) == 2
    t.save()
    # 重载：deferred 持久化 + 旧格式兼容（裸 list 也能读）
    t2 = EntityTracker.load(ws, pid)
    assert sorted(t2.deferred_names()) == ["丁", "戊"]
    # 出队：已介绍的不再推迟
    t2.clear_deferred(["char:3"])
    assert "char:3" not in t2.deferred


# ---------------------------------------------------------------- Chronicler paid_off

def test_chronicler_payoff(tmp_path):
    """收尾期 payoff=True：命中事件的 active 线 → paid_off + returned 落点。"""
    ws, pid = _project(tmp_path)
    _seed_volume(ws, pid)
    ch = Chronicler(ws, pid, llm=None)
    evs = [ExtractedEvent(summary="叶岚终于查明断玉佩封印的来历", kind="reveal")]
    report = ch.commit(evs, 1, 19, payoff=True)
    assert report.written == 1
    data = json.loads(ws._abs(f"{pid}/bible/plot_threads.json").read_text(encoding="utf-8"))
    yubi = next(t for t in data if t["id"] == "thread:yubi")
    assert yubi["status"] == "paid_off"
    assert yubi["returned"] == {"vol": 1, "ch": 19}
    # 行文期（payoff=False）：命中只推进 planted→active，不 paid_off
    _write(ws, pid, "bible/plot_threads.json", [
        {"id": "thread:yubi", "status": "planted", "desc": "断玉佩的来历与封印"}])
    ch.commit([ExtractedEvent(summary="叶岚摩挲断玉佩来历不明", kind="discovery")], 1, 5)
    data = json.loads(ws._abs(f"{pid}/bible/plot_threads.json").read_text(encoding="utf-8"))
    assert data[0]["status"] == "active"


def test_chronicler_payoff_book_scope_exempt(tmp_path):
    """收尾期 book 线豁免：卷末事件与全书主线关键词重合不等于主线落网（proj-t5 实测）。"""
    ws, pid = _project(tmp_path)
    _write(ws, pid, "bible/plot_threads.json", [
        {"id": "thread:guixuhui", "status": "active", "desc": "归墟会与血祭阴谋的幕后主使",
         "scope": "book"}])
    ch = Chronicler(ws, pid, llm=None)
    # ch5 明线"血祭图谋被遏制"与主线 desc 命中，但幕后主使未揭——不得 paid_off
    ch.commit([ExtractedEvent(summary="归墟会血祭图谋上报宗门护山大阵加固", kind="conflict")],
              1, 5, payoff=True)
    data = json.loads(ws._abs(f"{pid}/bible/plot_threads.json").read_text(encoding="utf-8"))
    assert data[0]["status"] == "active"


# ---------------------------------------------------------------- R-THREAD

def test_rthread_tail_warn_final_block(tmp_path):
    """收尾期未回收 → warn；卷末最后一章未回收 → block；book 线不查。"""
    ws, pid = _project(tmp_path)
    _seed_volume(ws, pid)
    # 卷内倒数第 2 章（19）的草稿已存在 → 判 TAIL 但非 final → warn
    p = ws.draft_path(pid, 1, 19)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("第一卷十九章正文", encoding="utf-8")
    alerts = [a for a in run_rule_checks(ws, pid) if a.rule_id == "R-THREAD"]
    levels = {a.object_ref: a.level for a in alerts}
    assert levels.get("thread:yubi") == "warn"
    assert "thread:mozun" not in levels  # book 线豁免

    # 写到最后一章（20）→ yubi 未收 → block
    p2 = ws.draft_path(pid, 1, 20)
    p2.write_text("第一卷二十章正文", encoding="utf-8")
    alerts = [a for a in run_rule_checks(ws, pid) if a.rule_id == "R-THREAD"]
    levels = {a.object_ref: a.level for a in alerts}
    assert levels.get("thread:yubi") == "block"

    # 回收后再查 → 无告警
    _write(ws, pid, "bible/plot_threads.json", [
        {"id": "thread:yubi", "status": "paid_off", "desc": "断玉佩的来历与封印",
         "scope": "volume", "target_vol": 1, "returned": {"vol": 1, "ch": 20}},
        {"id": "thread:mozun", "status": "active", "desc": "魔尊残魂注视人间",
         "scope": "book"},
        {"id": "thread:xieyi", "status": "active", "desc": "与云清瑶的约定",
         "scope": "volume", "target_vol": 2},
    ])
    alerts = [a for a in run_rule_checks(ws, pid) if a.rule_id == "R-THREAD"]
    assert alerts == []
