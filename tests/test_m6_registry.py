"""注册表体系 + 设定条目库 + R-ITEM 测试（讨论决策落地）。

覆盖：
- Registry：items/skills 双注册表加载、别名查找、canonical 归一化（残篇/残卷→规范名）
- worldstate.apply_delta：异名归一化入库不堆积；实力/战力/等级 映射 realm（都市高武适配）
- SettingIndex：pending 只返回「命中且未交代」；verify 置 revealed 并落盘
- produce_chapter 事件循环：生成时注入待交代设定、章末交代验证
- R-ITEM：同物异名告警、正文出现未持有物品告警
"""

from __future__ import annotations

import json

import pytest

from novelist.consistency import run_consistency
from novelist.core.llm import LLMResult
from novelist.core.session import SessionInfo
from novelist.storage.checkpoint import Checkpoint
from novelist.storage.workspace import Workspace


# ---------------------------------------------------------------- 工具


def _project(tmp_path) -> tuple[Workspace, str]:
    ws = Workspace(root=str(tmp_path))
    pid = "proj-t"
    ws.create_project(pid)
    Checkpoint(ws).save(pid, {"id": pid, "pipeline_state": "正文"})
    return ws, pid


def _write(ws, pid, rel, data):
    ws.write_json(ws._abs(f"{pid}/{rel}"), data)


def _seed_registry(ws, pid):
    _write(ws, pid, "bible/items.json", [
        {"id": "item:dan", "name": "洗髓丹", "type": "consumable", "aliases": ["洗髓丹药"]},
        {"id": "item:ling", "name": "灵石", "type": "currency", "aliases": []},
    ])
    _write(ws, pid, "bible/skills.json", [
        {"id": "skill:tai", "name": "太上忘情录", "type": "cultivation",
         "aliases": ["忘情录"], "state": "残篇"},
    ])


def _seed_settings(ws, pid):
    _write(ws, pid, "bible/settings.json", [
        {"id": "set:jingjie", "keywords": ["境界", "淬体", "内劲"], "text": "都市高武力体系：淬体→内劲→化神。",
         "revealed": False, "first_ch": 1},
        {"id": "set:gui", "keywords": ["诡异", "复苏"], "text": "诡异复苏：三年前灵气潮汐，诡异丛生。",
         "revealed": False, "first_ch": 1},
        {"id": "set:done", "keywords": ["青云宗"], "text": "青云宗已交代过的设定。",
         "revealed": True, "first_ch": 1},
    ])


class _SeqLLM:
    """顺序返回预设响应的 stub（生成/编纂/审校/润色各返回一个）。"""

    def __init__(self, rs):
        self.rs = list(rs)

    def complete(self, req):
        if not self.rs:
            return LLMResult(ok=True, content="", finish_reason="stop", provider="stub")
        return self.rs.pop(0)


def _res(text):
    return LLMResult(ok=True, content=text, finish_reason="stop", provider="stub")


# ---------------------------------------------------------------- Registry


def test_registry_load_and_canonical(tmp_path):
    ws, pid = _project(tmp_path)
    _seed_registry(ws, pid)
    from novelist.core.registry import Registry

    reg = Registry.load(ws, pid)
    assert set(reg.items) == {"item:dan", "item:ling"}
    assert set(reg.skills) == {"skill:tai"}

    assert reg.canonical("太上忘情录") == "太上忘情录"
    assert reg.canonical("忘情录") == "太上忘情录"      # 别名 → 规范名
    assert reg.canonical("《太上忘情录》") == "太上忘情录"  # 去书名号
    assert reg.canonical("不存在的东西") == "不存在的东西"   # 未注册原样返回


def test_registry_find_by_name(tmp_path):
    ws, pid = _project(tmp_path)
    _seed_registry(ws, pid)
    from novelist.core.registry import Registry

    reg = Registry.load(ws, pid)
    assert reg.find_by_name("洗髓丹").id == "item:dan"
    assert reg.find_by_name("洗髓丹药").id == "item:dan"   # 别名命中
    assert reg.find_by_name("灵石") is not None


# ---------------------------------------------------------------- worldstate 归一化


def test_apply_delta_normalizes_item_aliases(tmp_path):
    """同物异名（残篇/残卷）归一化到注册表规范名，不再堆积。"""
    ws, pid = _project(tmp_path)
    _seed_registry(ws, pid)
    from novelist.core.worldstate import apply_delta, load

    apply_delta(ws, pid, "char:a", {"获得": "《太上忘情录》残篇"}, {"vol": 1, "ch": 1})
    apply_delta(ws, pid, "char:a", {"获得": "太上忘情录残卷"}, {"vol": 1, "ch": 2})
    apply_delta(ws, pid, "char:a", {"获得": "洗髓丹药"}, {"vol": 1, "ch": 2})
    st = load(ws, pid)
    items = st["characters"]["char:a"]["items"]
    assert items.count("太上忘情录") == 1, f"同物异名应归一为一条，实际 {items}"
    assert "太上忘情录" in items and "残篇" not in "".join(items)
    assert "洗髓丹" in items and "洗髓丹药" not in "".join(items)


def test_apply_delta_realm_keys_urban(tmp_path):
    """都市高武适配：实力/战力/等级 都映射到 realm。"""
    ws, pid = _project(tmp_path)
    from novelist.core.worldstate import apply_delta, load

    apply_delta(ws, pid, "char:a", {"实力": "内劲大成"}, {"vol": 1, "ch": 1})
    apply_delta(ws, pid, "char:a", {"战力": "化神境"}, {"vol": 1, "ch": 2})
    st = load(ws, pid)
    assert st["characters"]["char:a"]["realm"] == "化神境"


# ---------------------------------------------------------------- SettingIndex


def test_settings_pending_only_unrevealed_hits(tmp_path):
    ws, pid = _project(tmp_path)
    _seed_settings(ws, pid)
    from novelist.core.settings import SettingIndex

    idx = SettingIndex.load(ws, pid)
    pend = idx.pending("主角进入诡异复苏的城市，淬体境武者出现", ch=1)
    ids = {e.id for e in pend}
    assert "set:jingjie" in ids      # 命中关键词且未交代
    assert "set:gui" in ids
    assert "set:done" not in ids      # 已交代的不再注入


def test_settings_verify_marks_revealed(tmp_path):
    ws, pid = _project(tmp_path)
    _seed_settings(ws, pid)
    from novelist.core.settings import SettingIndex

    idx = SettingIndex.load(ws, pid)
    done = idx.verify("夜色里诡异的气息弥漫，淬体境的小巷斗殴正在发生。")
    assert "set:jingjie" in done
    assert "set:gui" in done
    assert idx.entries[0].revealed is True
    idx.save()
    idx2 = SettingIndex.load(ws, pid)
    assert idx2.entries[0].revealed is True  # 落盘后可重载


# ---------------------------------------------------------------- 编排接入


def _seed_project_with_settings(ws, pid):
    _write(ws, pid, "bible/characters.json", [
        {"id": "char:zhu", "name": "林越", "gender": "male", "is_protagonist": True,
         "core_traits": ["扮猪吃虎"], "power": {"level": "化神"}},
        {"id": "char:xu", "name": "许晴", "gender": "female",
         "core_traits": ["飒爽"], "power": {"level": "淬体"}},
    ])
    _seed_registry(ws, pid)
    _seed_settings(ws, pid)
    g = ws.outline_chapter_path(pid, 1, 1)
    g.parent.mkdir(parents=True, exist_ok=True)
    g.write_text("key_events: [第一次遭遇诡异, 巷口对峙]\n"
                 "细纲：林越（淬体境伪装）在诡异复苏的城中偶遇许晴。", encoding="utf-8")


def test_event_loop_injects_pending_settings_and_verifies(tmp_path):
    """事件循环：生成 prompt 注入未交代设定；章末 verify 把交代的条目置位。"""
    ws, pid = _project(tmp_path)
    _seed_project_with_settings(ws, pid)
    from novelist.core.orchestrator import produce_chapter
    from novelist.core.settings import SettingIndex
    from novelist.storage.checkpoint import Checkpoint

    Checkpoint(ws).save(pid, {"id": pid, "pipeline_state": "正文"})
    llm = _SeqLLM([
        _res("夜色里诡异的气息弥漫，淬体境武者的小巷斗殴正在发生，他驻足。"),   # 事件1生成（带设定）
        _res("遭遇诡异 | conflict | 林越"),                              # 事件1编纂
        _res("许晴从巷口走出，两人对视，他依旧扮作普通人。"),                # 事件2生成
        _res("对峙 | dialogue | 林越,许晴"),                             # 事件2编纂
    ])
    res = produce_chapter(
        ws, pid, 1, 1, llm, prefer_direct=True, inject_bible=False, event_loop=True,
        commit_chapter_event=False, direct_words_floor=5,
        session=SessionInfo(project_id=pid, agent="t"))
    assert res.ok, res.result

    final = ws.draft_path(pid, 1, 1).read_text(encoding="utf-8")
    # 生成时注入过未交代设定（正文带出了淬体/诡异 → 章末验证置位）
    idx = SettingIndex.load(ws, pid)
    by_id = {e.id: e for e in idx.entries}
    assert by_id["set:jingjie"].revealed is True
    assert by_id["set:gui"].revealed is True


# ---------------------------------------------------------------- R-ITEM


def _seed_chapter_files(ws, pid, text):
    d = ws._abs(f"{pid}/chapters")
    d.mkdir(parents=True, exist_ok=True)
    (d / "1-1.md").write_text(text, encoding="utf-8")


def test_ritem_alias_mix(tmp_path):
    """R-ITEM：同一功法在正文出现多种叫法 → warn。"""
    ws, pid = _project(tmp_path)
    _seed_registry(ws, pid)
    _write(ws, pid, "bible/worldstate.json", {"characters": {
        "char:a": {"name": "林越", "items": ["太上忘情录"], "realm": "淬体",
                   "location": "", "injuries": [], "dead": False, "history": []}}})
    _seed_chapter_files(ws, pid, "林越翻开太上忘情录残篇，又念了句忘情录。")
    alerts = [a for a in run_consistency(ws, pid) if a.rule_id == "R-ITEM"]
    assert any("多种叫法" in a.detail for a in alerts), alerts


def test_ritem_unheld_item(tmp_path):
    """R-ITEM：正文出现注册表物品但无人持有 → warn。"""
    ws, pid = _project(tmp_path)
    _seed_registry(ws, pid)
    _write(ws, pid, "bible/worldstate.json", {"characters": {
        "char:a": {"name": "林越", "items": [], "realm": "淬体",
                   "location": "", "injuries": [], "dead": False, "history": []}}})
    _seed_chapter_files(ws, pid, "林越服下一枚洗髓丹。")
    alerts = [a for a in run_consistency(ws, pid) if a.rule_id == "R-ITEM"]
    assert any("无任何人持有" in a.detail for a in alerts), alerts


def test_ritem_clean_when_consistent(tmp_path):
    ws, pid = _project(tmp_path)
    _seed_registry(ws, pid)
    _write(ws, pid, "bible/worldstate.json", {"characters": {
        "char:a": {"name": "林越", "items": ["洗髓丹", "太上忘情录"], "realm": "淬体",
                   "location": "", "injuries": [], "dead": False, "history": []}}})
    _seed_chapter_files(ws, pid, "林越服下洗髓丹，体内响起太上忘情录的运转声。")
    alerts = [a for a in run_consistency(ws, pid) if a.rule_id == "R-ITEM"]
    assert alerts == [], alerts
