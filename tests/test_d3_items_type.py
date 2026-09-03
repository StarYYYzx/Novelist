"""D3 回归：bible/items schema 必填 `type`（英文枚举）而生成/摄入只给中文类别。

背景（2026-09-03 新书 5 章实测）：通用包 book 节点 prompt 让模型填 `category`，
ingest 摄入写 `category`（源自 kind）——items.schema additionalProperties=false 白名单
会把 category 剥离，`type` 无人填 → V1 `'type' is a required property` block。
修复 = ①book 协议行改要 type 枚举；②sync_bible 导出口 `_normalize_item_type`
按中文词表归一化（ingest 路径同经此口，无需单独改）。
"""

from __future__ import annotations

import json

from novelist.core.bible import validate_project
from novelist.forge.nodes import _normalize_item_type, sync_bible
from novelist.forge.state import Blueprint

SEED_META = {"title": "t", "genre": "修仙", "logline": "x",
             "scale": {"volumes": 1, "chapters_per_volume": 1, "target_words_per_chapter": 100}}


def test_normalize_item_type_enum_passthrough():
    assert _normalize_item_type({"type": "Artifact"}) == "artifact"  # 大小写宽容
    assert _normalize_item_type({"category": "consumable"}) == "consumable"


def test_normalize_item_type_chinese_keyword_map():
    assert _normalize_item_type({"category": "法宝"}) == "artifact"
    assert _normalize_item_type({"kind": "丹药"}) == "consumable"
    assert _normalize_item_type({"category": "货币", "name": "下品灵石"}) == "currency"
    assert _normalize_item_type({"category": "材料"}) == "material"
    assert _normalize_item_type({"name": "青锋剑"}) == "equipment"  # 名称兜底扫描


def test_normalize_item_type_fallback_other():
    assert _normalize_item_type({"category": " unknown "}) == "other"
    assert _normalize_item_type({"name": "神秘小盒子"}) == "other"


def test_sync_bible_items_all_carry_valid_type(ws_factory):
    """LLM 路径：蓝图条目只有中文 category → 落盘后 type 合法且无 category。"""
    ws, pid = ws_factory("proj-d3a")
    bp = Blueprint.blank(dict(SEED_META))
    bp.upsert("items", {"id": "item:1", "name": "回春丹", "category": "丹药"})
    bp.upsert("items", {"id": "item:2", "name": "玄天镜", "category": "法宝"})
    bp.upsert("items", {"id": "item:3", "name": "神秘小盒子"})
    sync_bible(ws, pid, bp)
    items = json.loads(ws._abs(f"{pid}/bible/items.json").read_text(encoding="utf-8"))  # noqa: SLF001
    assert [it["type"] for it in items] == ["consumable", "artifact", "other"]
    assert all("category" not in it for it in items)
    assert validate_project(ws, pid) == []  # V1 契约全过


def test_sync_bible_items_ingest_path(ws_factory):
    """ingest 路径：kind 源类别同样经 sync_bible 归一化（ingest.py 写 category 不变）。"""
    ws, pid = ws_factory("proj-d3b")
    bp = Blueprint.blank(dict(SEED_META))
    bp.upsert("items", {"id": "item:9", "name": "储物袋", "kind": "法器"})
    sync_bible(ws, pid, bp)
    assert validate_project(ws, pid) == []
