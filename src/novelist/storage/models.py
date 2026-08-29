"""存储层：实体建模与写入校验（docs/06 §5，ADR-011）。

- `SchemaRegistry.load(name)`：装载 schemas/ 目录下的 JSON Schema。
- `SchemaRegistry.validate(name, data)`：用 JSON Schema 校验数据（写库前校验）。
- 核心实体 Pydantic 模型（人物卡/伏笔/时间线等），供读取/校验/序列化用。

schema 源目录默认 `schemas/`（仓库根），可通过 `NOVELIST_SCHEMAS` 环境变量覆盖。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # Pydantic 可选依赖（pyproject dependencies 含 pydantic>=2）
    from pydantic import BaseModel, Field  # type: ignore
    _HAS_PYDANTIC = True
except ImportError:  # pragma: no cover - 未安装时仍可用 JSON Schema 校验
    _HAS_PYDANTIC = False


def _default_schemas_root() -> Path:
    env = os.getenv("NOVELIST_SCHEMAS")
    if env:
        return Path(env).resolve()
    # 本模块位于 src/novelist/storage/models.py；仓库根 schemas/ 为上级的上级的父
    return Path(__file__).resolve().parents[3] / "schemas"


class SchemaError(Exception):
    pass


@dataclass
class SchemaRegistry:
    """装载并缓存 JSON Schema。"""

    root: Path = field(default_factory=_default_schemas_root)
    _cache: dict[str, dict] = field(default_factory=dict, repr=False)

    def load(self, name: str) -> dict:
        """按名称装载 schema。name 形如 'bible/characters'。"""
        if name in self._cache:
            return self._cache[name]
        candidate = self.root / f"{name}.schema.json"
        if not candidate.exists():
            raise SchemaError(f"schema not found: {name}")
        with open(candidate, "r", encoding="utf-8") as f:
            schema = json.load(f)
        self._cache[name] = schema
        return schema

    def validate(self, name: str, data: Any) -> None:
        """用 JSON Schema 校验数据；不通过抛 SchemaError。"""
        schema = self.load(name)
        try:
            import jsonschema  # type: ignore
        except ImportError:  # pragma: no cover - jsonschema 在 pyproject dependencies
            raise SchemaError("jsonschema not installed")
        try:
            jsonschema.validate(instance=data, schema=schema)
        except jsonschema.ValidationError as e:  # type: ignore
            path = ".".join(str(p) for p in (e.absolute_path or []))
            raise SchemaError(f"schema {name} violated at {path}: {e.message}") from e


# ---- 核心实体 Pydantic 模型（docs/06 §3，供给读/写自动校验） ----
if _HAS_PYDANTIC:  # pragma: no cover - 取决于依赖

    class ChapterRef(BaseModel):
        vol: int
        ch: int

    class Character(BaseModel):
        id: str
        name: str
        status: str = "active"
        aliases: list[str] = Field(default_factory=list)
        core_traits: list[str] = Field(default_factory=list)

    class PlotThread(BaseModel):
        id: str
        desc: str
        status: str = "planted"  # unplanned|planted|pending_return|returned

    class TimelineEvent(BaseModel):
        id: str
        at: dict[str, Any]
        event: str

    class PlotEvent(BaseModel):
        id: str
        at: dict[str, Any]
        type: str
        summary: str

else:  # pragma: no cover
    Character = None  # type: ignore
    PlotThread = None  # type: ignore
    TimelineEvent = None  # type: ignore
    PlotEvent = None  # type: ignore
