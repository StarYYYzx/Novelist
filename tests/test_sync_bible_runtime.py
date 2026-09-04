"""sync_bible 运行态合并（2026-09-04 实证缺陷修复）。

实证：proj-20260903194907 ch1-5 生成期 verify 已把 settings.json 置 revealed=true，
21:35 蓝图重同步全量覆盖 → revealed 全部回 false，交代状态机空转。同面受害：
plot_threads 流转（chronicler）、items/skills 状态域、characters 上 enrich/工厂补喂
的扩展键与工厂注册的新卡（characters/locations）。

契约（ADR-016 文件=事实源）：
- 蓝图计划字段胜；白名单运行态字段盘上值胜；
- characters/locations/settings 保留盘上独有键与独有行（keep_extra）；
- 盘上无对应文件/坏 JSON 时行为与旧版一致（纯蓝图落盘）。
"""

from __future__ import annotations

import json

import pytest

from novelist.forge.nodes import sync_bible
from novelist.forge.state import Blueprint

SEED_META = {"title": "t", "genre": "修仙", "logline": "x",
             "scale": {"volumes": 1, "chapters_per_volume": 1, "target_words_per_chapter": 100}}


def _bp() -> Blueprint:
    bp = Blueprint.blank(dict(SEED_META))
    bp.data["worldview"] = {"name": "测试界", "power_system": {"levels": ["练气"]}}
    return bp


def _read(ws, pid, rel):
    return json.loads(ws._abs(f"{pid}/{rel}").read_text(encoding="utf-8"))  # noqa: SLF001


def _write_disk(ws, pid, rel, rows):
    ws._abs(f"{pid}/{rel}").write_text(  # noqa: SLF001
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def test_settings_revealed_survives_resync(ws_factory):
    """核心回归：revealed=true 的运行态在蓝图重同步后不被重置。"""
    ws, pid = ws_factory("proj-rt1")
    bp = _bp()
    bp.upsert("settings", {"id": "set:a", "keywords": ["甲"], "text": "设定甲"})
    sync_bible(ws, pid, bp)
    # 生成期 verify 置 revealed（模拟：直接改盘）
    rows = _read(ws, pid, "bible/settings.json")
    rows[0]["revealed"] = True
    _write_disk(ws, pid, "bible/settings.json", rows)
    # 蓝图修订后重同步
    bp.upsert("settings", {"id": "set:a", "keywords": ["甲", "乙"], "text": "设定甲·修订"})
    sync_bible(ws, pid, bp)
    out = _read(ws, pid, "bible/settings.json")
    assert out[0]["revealed"] is True          # 运行态保留
    assert out[0]["keywords"] == ["甲", "乙"]  # 蓝图计划字段胜
    assert out[0]["text"] == "设定甲·修订"


def test_threads_flow_state_survives_resync(ws_factory):
    """chronicler 流转的 status/planted/returned 不被蓝图重置。"""
    ws, pid = ws_factory("proj-rt2")
    bp = _bp()
    bp.upsert("threads", {"id": "pt:1", "desc": "伏笔一"})
    sync_bible(ws, pid, bp)
    rows = _read(ws, pid, "bible/plot_threads.json")
    rows[0].update({"status": "active", "planted": 3, "returned": 0})
    _write_disk(ws, pid, "bible/plot_threads.json", rows)
    bp.upsert("threads", {"id": "pt:1", "desc": "伏笔一·修订"})
    sync_bible(ws, pid, bp)
    row = _read(ws, pid, "bible/plot_threads.json")[0]
    assert row["status"] == "active" and row["planted"] == 3
    assert row["desc"] == "伏笔一·修订"


def test_characters_keep_extra_and_disk_only(ws_factory):
    """enrich 扩展键（provenance/behavior_rules）与工厂注册卡不被覆盖。"""
    ws, pid = ws_factory("proj-rt3")
    bp = _bp()
    bp.upsert("characters", {"id": "char:a", "name": "甲", "role": "protagonist",
                             "core_traits": []})
    sync_bible(ws, pid, bp)
    rows = _read(ws, pid, "bible/characters.json")
    rows[0]["provenance"] = {"origin": "enrich"}
    rows[0]["behavior_rules"] = ["不许笑"]
    rows.append({"id": "char:fac1", "name": "工厂卡", "status": "active"})
    _write_disk(ws, pid, "bible/characters.json", rows)
    bp.upsert("characters", {"id": "char:a", "name": "甲改名", "role": "protagonist",
                             "core_traits": ["狠"]})
    sync_bible(ws, pid, bp)
    out = {c["id"]: c for c in _read(ws, pid, "bible/characters.json")}
    assert out["char:a"]["name"] == "甲改名"            # 蓝图胜
    assert out["char:a"]["provenance"]["origin"] == "enrich"  # 盘上扩展键保留
    assert out["char:a"]["behavior_rules"] == ["不许笑"]
    assert out["char:a"]["is_protagonist"] is True
    assert "char:fac1" in out                           # 盘上独有行保留


def test_locations_disk_only_factory_rows_kept(ws_factory):
    """工厂注册的 locations 独有行在重同步后保留。"""
    ws, pid = ws_factory("proj-rt4")
    bp = _bp()
    bp.upsert("locations", {"id": "loc:1", "name": "主城"})
    sync_bible(ws, pid, bp)
    _write_disk(ws, pid, "bible/locations.json",
                [{"id": "loc:1", "name": "主城"},
                 {"id": "loc:fac2", "name": "工厂场景", "status": "active"}])
    sync_bible(ws, pid, bp)
    ids = [r["id"] for r in _read(ws, pid, "bible/locations.json")]
    assert ids == ["loc:1", "loc:fac2"]


def test_items_state_survives_but_disk_only_dropped(ws_factory):
    """items：状态域保留；keep_extra=False 时盘上独有行不保留（仅蓝图行）。"""
    ws, pid = ws_factory("proj-rt5")
    bp = _bp()
    bp.upsert("items", {"id": "item:1", "name": "灵石", "type": "other"})
    sync_bible(ws, pid, bp)
    rows = _read(ws, pid, "bible/items.json")
    rows[0]["state"] = "已消耗"
    rows.append({"id": "item:ghost", "name": "幽灵"})
    _write_disk(ws, pid, "bible/items.json", rows)
    sync_bible(ws, pid, bp)
    out = _read(ws, pid, "bible/items.json")
    assert out[0]["state"] == "已消耗"
    assert all(r["id"] != "item:ghost" for r in out)


def test_resync_without_disk_state_is_unchanged(ws_factory):
    """盘上无运行态（首次同步）时与旧行为完全一致。"""
    ws, pid = ws_factory("proj-rt6")
    bp = _bp()
    bp.upsert("characters", {"id": "char:a", "name": "甲", "role": "protagonist",
                             "core_traits": []})
    bp.upsert("settings", {"id": "set:a", "keywords": ["甲"], "text": "甲"})
    sync_bible(ws, pid, bp)
    chars = _read(ws, pid, "bible/characters.json")
    assert chars[0]["status"] == "active" and chars[0]["is_protagonist"] is True
    assert _read(ws, pid, "bible/settings.json")[0]["revealed"] is False


@pytest.mark.parametrize("bad", [None])
def test_resync_with_corrupt_disk_file_falls_back(ws_factory, bad):
    """盘上坏 JSON → 忽略盘上状态，纯蓝图落盘（不抛异常）。"""
    ws, pid = ws_factory("proj-rt7")
    bp = _bp()
    bp.upsert("settings", {"id": "set:a", "keywords": ["甲"], "text": "甲"})
    sync_bible(ws, pid, bp)
    ws._abs(f"{pid}/bible/settings.json").write_text("{broken", encoding="utf-8")  # noqa: SLF001
    sync_bible(ws, pid, bp)
    assert _read(ws, pid, "bible/settings.json")[0]["id"] == "set:a"
