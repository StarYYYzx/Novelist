"""Genre Pack 装载（docs/10 §8）。

一个类型包 = 一种小说类型的全部构建知识（槽位覆盖/抽取词表/节奏/默认文风）。
数据驱动：新增类型 = 往 `genres/` 加一个 JSON，代码零改动。
装载时用 `schemas/forge/genres.schema.json` 校验（V1：类型包自身可校验）。
"""

from __future__ import annotations

import json
from pathlib import Path

from ..storage.models import SchemaError, SchemaRegistry

GENRES_DIR = Path(__file__).resolve().parent / "genres"
GENERIC_ID = "通用"

_CACHE: dict[str, dict] = {}


def _pack_path(pack_id: str) -> Path:
    # 仅允许安全字符，防路径注入
    if not pack_id or any(c in pack_id for c in "/\\:*?\"<>|"):
        raise ValueError(f"invalid genre pack id: {pack_id!r}")
    return GENRES_DIR / f"{pack_id}.json"


def load_pack(pack_id: str, *, validate: bool = True) -> dict:
    """按 id 装载类型包；缺失抛 KeyError。"""
    if pack_id in _CACHE:
        return _CACHE[pack_id]
    path = _pack_path(pack_id)
    if not path.exists():
        raise KeyError(f"genre pack not found: {pack_id}")
    with open(path, "r", encoding="utf-8") as f:
        pack = json.load(f)
    if validate:
        try:
            SchemaRegistry().validate("forge/genres", pack)
        except SchemaError as e:
            raise ValueError(f"genre pack {pack_id} 未过自身 schema 校验: {e}") from e
    _CACHE[pack_id] = pack
    return pack


def load_pack_for(genre: str, *, validate: bool = True) -> dict:
    """按类型名装载：先精确 id，再别名匹配；库内无此类型 → 通用包兜底。"""
    if not genre:
        return load_pack(GENERIC_ID, validate=validate)
    try:
        return load_pack(genre, validate=validate)
    except KeyError:
        pass
    # 别名匹配（大小写不敏感）
    genre_lower = genre.strip().lower()
    for pack_id in list_packs():
        pack = load_pack(pack_id, validate=False)
        if any(str(a).lower() == genre_lower for a in pack.get("aliases", [])):
            return pack
    return load_pack(GENERIC_ID, validate=validate)


def list_packs() -> list[str]:
    """库内全部类型包 id（不含 .json 后缀，按文件名排序）。"""
    return sorted(p.stem for p in GENRES_DIR.glob("*.json"))


def clear_cache() -> None:
    _CACHE.clear()
