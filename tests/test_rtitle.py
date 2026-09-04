"""R-TITLE 称谓一致性规则（至高职位、视角无关子集）测试。

设计要点（2026-09-04 用户拍板）：
- 师承/同门类**视角相对称谓**（师尊/师兄/徒儿…）刻意不查——"自己的师姐在大师兄
  口中是师妹、在师尊口中是徒儿"，任一合法视角能解释即放行；
- 只查视角无关的**至高职位**（掌门/宗主/家主…）：不论谁开口都指向同一人；
- ①bible 内部：同一至高职位多人声称 → warn；②正文：职位紧邻冠到他人头上 → warn；
- 豁免（伪装剧情）：bible/title_rules.json。
"""

from __future__ import annotations

import json

from novelist.consistency.rules import _title_check
from novelist.storage.checkpoint import Checkpoint
from novelist.storage.workspace import Workspace


def _project(tmp_path, pid="proj-title"):
    ws = Workspace(root=str(tmp_path))
    ws.create_project(pid)
    Checkpoint(ws).save(pid, {"id": pid, "title": "测试书", "pipeline_state": "正文",
                              "event_seq": 0})
    (ws._abs(f"{pid}/drafts/chapters")).mkdir(parents=True, exist_ok=True)
    return ws, pid


def _chars(ws, pid, chars):
    ws.write_json(ws._abs(f"{pid}/bible/characters.json"), chars)


def _chapter(ws, pid, name, text):
    (ws._abs(f"{pid}/drafts/chapters") / name).write_text(text, encoding="utf-8")


def _alerts(ws, pid):
    return [(a.rule_id, a.object_ref, a.detail) for a in _title_check(ws, pid)]


def test_bible_conflict_same_supreme_title(tmp_path):
    """两个角色都声称掌门 → warn。"""
    ws, pid = _project(tmp_path)
    _chars(ws, pid, [
        {"id": "c1", "name": "青云子", "role": "掌门"},
        {"id": "c2", "name": "段无涯", "role": "掌门"},
    ])
    alerts = _alerts(ws, pid)
    assert any(rid == "R-TITLE" and o == "掌门" for rid, o, _d in alerts)


def test_text_calls_wrong_person_supreme(tmp_path):
    """正文「掌门段无涯」而 bible 掌门是青云子 → warn 指向段无涯。"""
    ws, pid = _project(tmp_path)
    _chars(ws, pid, [
        {"id": "c1", "name": "青云子", "role": "掌门"},
        {"id": "c2", "name": "段无涯", "role": "执法长老"},
    ])
    _chapter(ws, pid, "1-1.md", "掌门段无涯踏入大殿，众弟子噤声。")
    alerts = _alerts(ws, pid)
    assert any(r == "R-TITLE" and o == "段无涯" for r, o, _d in alerts)


def test_owner_reference_not_alerted(tmp_path):
    """正文称呼合法持有者本人（青云子掌门）不报。"""
    ws, pid = _project(tmp_path)
    _chars(ws, pid, [
        {"id": "c1", "name": "青云子", "role": "掌门"},
        {"id": "c2", "name": "段无涯", "role": "执法长老"},
    ])
    _chapter(ws, pid, "1-1.md", "青云子掌门拂尘一摆：\"掌门段无涯之事，容后再议。\"")
    alerts = [a for a in _alerts(ws, pid) if a[0] == "R-TITLE"]
    # 段无涯被错称（该报），青云子本人被正确称呼（不该报）
    assert any(o == "段无涯" for _r, o, _d in alerts)
    assert not any(o == "青云子" for _r, o, _d in alerts)


def test_distant_mention_not_alerted(tmp_path):
    """名与职位不相邻的叙述（「段无涯在掌门大选中…」）不误报。"""
    ws, pid = _project(tmp_path)
    _chars(ws, pid, [
        {"id": "c1", "name": "青云子", "role": "掌门"},
        {"id": "c2", "name": "段无涯", "role": "执法长老"},
    ])
    _chapter(ws, pid, "1-1.md", "段无涯在掌门大选中投了弃权票。")
    assert _alerts(ws, pid) == []


def test_perspective_kinship_not_checked(tmp_path):
    """视角相对称谓（师妹/徒儿）不进检查面——任何人称都不报。"""
    ws, pid = _project(tmp_path)
    _chars(ws, pid, [
        {"id": "c1", "name": "林婉儿", "role": "外门弟子"},
        {"id": "c2", "name": "大师兄", "role": "内门首席"},
    ])
    _chapter(ws, pid, "1-1.md", "大师兄唤她师妹，师尊唤她徒儿，她自己只认师姐这个称呼。")
    assert _alerts(ws, pid) == []


def test_exempt_whitelist(tmp_path):
    """title_rules.json 豁免（伪装剧情）→ 不报。"""
    ws, pid = _project(tmp_path)
    _chars(ws, pid, [
        {"id": "c1", "name": "青云子", "role": "掌门"},
        {"id": "c2", "name": "段无涯", "role": "执法长老"},
    ])
    _chapter(ws, pid, "1-1.md", "掌门段无涯端坐主位——无人知这是易容伪装。")
    ws.write_json(ws._abs(f"{pid}/bible/title_rules.json"),
                  {"exempt": [{"name": "段无涯", "title": "掌门", "note": "伪装剧情"}]})
    assert _alerts(ws, pid) == []


def test_no_role_cards_inert(tmp_path):
    """无 role 字段的卡（如都市书实测 role=None）→ 规则静默不误报。"""
    ws, pid = _project(tmp_path)
    _chars(ws, pid, [
        {"id": "c1", "name": "李天劫", "power": {"level": "渡劫圆满", "faction": "无"}},
        {"id": "c2", "name": "苏老", "power": {"level": "金丹期", "faction": "协会"}},
    ])
    _chapter(ws, pid, "1-1.md", "会长苏老亲临现场，协会会长亲自授牌。")
    assert _alerts(ws, pid) == []
