"""Forge 状态层（docs/10 §4）：Blueprint 读写 + provenance + ForgeState。

- `Blueprint`：构建层唯一中间态（`workspace/forge/blueprint.json`）。
  字段 = 下游产出超集 + provenance。读写均过 `schemas/forge/blueprint.schema.json` 校验
  （V1：蓝图自身可校验，手改打错键名在 build 前爆）。
- `ForgeState`：`project.json.forge` 段（mode/interaction/stage/blueprint_rev/calls_used）。
- `append_transcript`：问答/构建留痕（`workspace/forge/transcript.jsonl`，中断续跑与审计用）。

路径语法：点路径 + 数组下标，如 `meta.logline`、`characters[0].name`；
provenance 键兼容 `characters[char:x].name` 的 id 索引写法（docs/10 §4.2 样例）。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..storage.models import SchemaError, SchemaRegistry
from ..storage.workspace import Workspace, WorkspaceError

FORGE_REL = "workspace/forge"
BLUEPRINT_FILE = "blueprint.json"
TRANSCRIPT_FILE = "transcript.jsonl"

_IDX_RE = re.compile(r"^(.*?)\[(\d+)\]$")
PROV_SRC = ("user", "llm", "template", "ingested")


# ---- 点路径工具（支持 a.b[0].c）----
def _split_key(key: str) -> list[str | int]:
    parts: list[str | int] = []
    for piece in key.split("."):
        m = _IDX_RE.match(piece)
        if m:
            parts.append(m.group(1))
            parts.append(int(m.group(2)))
        else:
            parts.append(piece)
    return parts


def get_path(data: dict, path: str, default: Any = None) -> Any:
    cur: Any = data
    try:
        for k in _split_key(path):
            cur = cur[k]
    except (KeyError, IndexError, TypeError):
        return default
    return cur


def set_path(data: dict, path: str, value: Any) -> None:
    parts = _split_key(path)
    cur: Any = data
    for k in parts[:-1]:
        if isinstance(k, int):
            cur = cur[k]
        else:
            nxt = cur.get(k)
            if not isinstance(nxt, (dict, list)):
                nxt = {} if not isinstance(parts[parts.index(k) + 1], int) else []
                cur[k] = nxt
            cur = nxt
    last = parts[-1]
    if isinstance(last, int):
        cur[last] = value
    else:
        cur[last] = value


def _norm_prov_path(path: str) -> str:
    """provenance 键归一：`characters[char:x].name` 视为 `characters[<id>].name` 形态，直接存原文。"""
    return path


# ---- 模型输出枚举归一（2026-09-05 云端 Qwen3.8-27B 真机实证）----
# 模型会把 bible 侧行文态（plot_threads.status="active"）写进蓝图 threads，
# 或输出同义英文/中文变体——枚举外值在 bp.save 的 SchemaError 直接崩掉整次构建。
# 归一放在 validate() 入口（save/load 双通道覆盖），只改非法值，合法值原样通过。
_THREAD_STATUS = {"unplanned", "planted", "pending_return", "returned"}
_THREAD_STATUS_ALIASES = {
    # bible 行文态 / 同义变体 → 蓝图最近合法态（已登记待埋设=planted）
    "active": "planted", "ongoing": "planted", "in_progress": "planted",
    "inprogress": "planted", "open": "planted", "live": "planted",
    "进行中": "planted", "激活": "planted", "已埋": "planted",
    "planned": "unplanned", "todo": "unplanned", "none": "unplanned",
    "计划中": "unplanned", "未埋": "unplanned",
    "pending": "pending_return", "pendingreturn": "pending_return",
    "待回收": "pending_return",
    "done": "returned", "resolved": "returned", "closed": "returned",
    "paid_off": "returned", "paidoff": "returned", "paid": "returned",
    "completed": "returned", "complete": "returned", "finished": "returned",
    "已回收": "returned", "已兑现": "returned",
}
_THREAD_SCOPE = {"volume", "book"}
_THREAD_SCOPE_ALIASES = {"vol": "volume", "卷": "volume", "volume_wide": "volume",
                         "全书": "book", "series": "book", "book_wide": "book"}
_CHAR_STATUS = {"active", "dead", "away", "unknown"}
_CHAR_STATUS_ALIASES = {"alive": "active", "present": "active", "in_scene": "active",
                        "在场": "active", "missing": "away", "absent": "away",
                        "离场": "away", "deceased": "dead", "死亡": "dead"}
_GENDER = {"male", "female", "unknown"}
_GENDER_ALIASES = {"man": "male", "m": "male", "男": "male",
                   "woman": "female", "f": "female", "女": "female"}
_ROLE = {"protagonist", "mentor", "rival", "love_interest", "minor"}
_ROLE_ALIASES = {"main": "protagonist", "主角": "protagonist",
                 "master": "mentor", "teacher": "mentor", "师父": "mentor", "导师": "mentor",
                 "villain": "rival", "antagonist": "rival", "反派": "rival", "对手": "rival",
                 "love": "love_interest", "loveinterest": "love_interest",
                 "恋人": "love_interest", "女主": "love_interest", "男主": "love_interest",
                 "supporting": "minor", "配角": "minor", "龙套": "minor"}
_SRC = {"user", "llm", "template", "ingested"}
_SRC_ALIASES = {"model": "llm", "ai": "llm", "auto": "llm", "system": "template"}


def _coerce_enum(value: Any, valid: set[str], aliases: dict[str, str], default: str) -> str:
    """非法枚举值归一：合法原样通过；别名表映射；其余落 default。幂等。"""
    if isinstance(value, str):
        v = value.strip().lower()
        if v in valid:
            return v
        if v in aliases:
            return aliases[v]
    return default


def _coerce_src_deep(node: Any) -> None:
    """递归归一所有 `src` 字段（slot 应答/条目级 provenance 都可能被模型写歪）。"""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "src" and not (isinstance(v, str) and v.strip().lower() in _SRC):
                node[k] = _coerce_enum(v, _SRC, _SRC_ALIASES, "llm")
            else:
                _coerce_src_deep(v)
    elif isinstance(node, list):
        for item in node:
            _coerce_src_deep(item)


# ---- Blueprint ----
@dataclass
class Blueprint:
    """蓝图中间态。`data` 为全量 dict（与 schema 对齐）。"""

    data: dict = field(default_factory=dict)

    # ---- 构造 ----
    @classmethod
    def blank(cls, meta: dict | None = None) -> "Blueprint":
        data = {
            "rev": 1,
            "provenance": {},
            "meta": meta or {},
            "worldview": {},
            "characters": [],
            "locations": [],
            "items": [],
            "skills": [],
            "settings": [],
            "threads": [],
            "style": {},
            "volumes": [],
            "chapters": [],
        }
        return cls(data)

    @classmethod
    def load(cls, ws: Workspace, project_id: str) -> "Blueprint":
        path = ws._abs(f"{project_id}/{FORGE_REL}/{BLUEPRINT_FILE}")  # noqa: SLF001
        if not path.exists():
            raise FileNotFoundError(f"blueprint not found: {path}")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        bp = cls(data)
        bp.validate()
        return bp

    def save(self, ws: Workspace, project_id: str) -> Path:
        self.validate()
        path = ws._abs(f"{project_id}/{FORGE_REL}/{BLUEPRINT_FILE}")  # noqa: SLF001
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        tmp.replace(path)
        return path

    def normalize(self) -> None:
        """枚举字段防御归一（幂等，只动非法值）——先于 schema 校验执行。"""
        threads = self.data.get("threads")
        if isinstance(threads, list):
            for t in threads:
                if isinstance(t, dict):
                    t["status"] = _coerce_enum(t.get("status"), _THREAD_STATUS,
                                               _THREAD_STATUS_ALIASES, "unplanned")
                    t["scope"] = _coerce_enum(t.get("scope"), _THREAD_SCOPE,
                                              _THREAD_SCOPE_ALIASES, "book")
        chars = self.data.get("characters")
        if isinstance(chars, list):
            for c in chars:
                if isinstance(c, dict):
                    c["status"] = _coerce_enum(c.get("status"), _CHAR_STATUS,
                                               _CHAR_STATUS_ALIASES, "unknown")
                    c["gender"] = _coerce_enum(c.get("gender"), _GENDER,
                                               _GENDER_ALIASES, "unknown")
                    c["role"] = _coerce_enum(c.get("role"), _ROLE,
                                             _ROLE_ALIASES, "minor")
        _coerce_src_deep(self.data)

    def validate(self) -> None:
        self.normalize()
        try:
            SchemaRegistry().validate("forge/blueprint", self.data)
        except SchemaError as e:
            raise ValueError(f"blueprint 未过自身 schema 校验: {e}") from e

    def bump_rev(self) -> int:
        self.data["rev"] = int(self.data.get("rev", 0)) + 1
        return self.data["rev"]

    # ---- 字段访问 ----
    def get(self, path: str, default: Any = None) -> Any:
        return get_path(self.data, path, default)

    def set(self, path: str, value: Any) -> None:
        set_path(self.data, path, value)

    def section(self, name: str) -> list:
        """取数组段（characters/locations/…），缺省建空。"""
        if name not in self.data or not isinstance(self.data[name], list):
            self.data[name] = []
        return self.data[name]

    def find_by_id(self, section: str, obj_id: str) -> dict | None:
        return next((x for x in self.section(section) if x.get("id") == obj_id), None)

    def upsert(self, section: str, item: dict) -> None:
        """按 id upsert（重跑幂等：人物/伏笔/设定按 id 覆盖，不产生重复条目）。"""
        items = self.section(section)
        for i, x in enumerate(items):
            if x.get("id") == item.get("id"):
                items[i] = item
                return
        items.append(item)

    # ---- provenance ----
    def set_provenance(self, path: str, src: str, confidence: float, evidence: str | None = None) -> None:
        if src not in PROV_SRC:
            raise ValueError(f"invalid provenance src: {src!r}")
        confidence = float(confidence)
        if not 0 <= confidence <= 1:
            raise ValueError(f"confidence out of range: {confidence}")
        prov = self.data.setdefault("provenance", {})
        prov[_norm_prov_path(path)] = {
            "src": src,
            "confidence": confidence,
            **( {"evidence": evidence} if evidence else {}),
        }

    def get_provenance(self, path: str) -> dict | None:
        prov = self.data.get("provenance", {})
        return prov.get(_norm_prov_path(path))

    def is_protected(self, path: str) -> bool:
        """src=user 的字段永不被模型产物覆盖（provenance 保护，docs/10 §7.6）。"""
        p = self.get_provenance(path)
        return bool(p and p.get("src") == "user")

    def provenance_summary(self) -> dict[str, int]:
        counts: dict[str, int] = {s: 0 for s in PROV_SRC}
        for p in self.data.get("provenance", {}).values():
            s = p.get("src")
            if s in counts:
                counts[s] += 1
        return counts

    def low_confidence_paths(self, threshold: float = 0.6) -> list[str]:
        """src=llm 且 confidence < 阈值的路径（递归时优先细化，docs/10 §4.2②）。"""
        out = []
        for path, p in self.data.get("provenance", {}).items():
            if p.get("src") == "llm" and p.get("confidence", 1.0) < threshold:
                out.append(path)
        return sorted(out)

    # ---- 缺口辅助 ----
    def filled(self, path: str) -> bool:
        """路径非空即视为已填（空串/空数组/None 都算未填）。"""
        v = self.get(path, None)
        if v is None:
            return False
        if isinstance(v, str):
            return bool(v.strip())
        if isinstance(v, (list, dict)):
            return len(v) > 0
        return True


# ---- ForgeState（project.json.forge 段）----
@dataclass
class ForgeState:
    mode: str = "seed"
    interaction: str = "auto"
    stage: str = "slots"
    blueprint_rev: int = 1
    calls_used: int = 0
    started_at: str | None = None
    last_error: str | None = None

    @classmethod
    def load(cls, ws: Workspace, project_id: str) -> "ForgeState":
        path = ws.project_json_path(project_id)
        try:
            data = ws.read_json(project_id, path)
        except WorkspaceError:
            data = {}
        f = data.get("forge", {}) if isinstance(data, dict) else {}
        return cls(
            mode=f.get("mode", "seed"),
            interaction=f.get("interaction", "auto"),
            stage=f.get("stage", "slots"),
            blueprint_rev=int(f.get("blueprint_rev", 1)),
            calls_used=int(f.get("calls_used", 0)),
            started_at=f.get("started_at"),
            last_error=f.get("last_error"),
        )

    def save(self, ws: Workspace, project_id: str) -> None:
        path = ws.project_json_path(project_id)
        try:
            data = ws.read_json(project_id, path)
        except WorkspaceError:
            data = {"id": project_id, "title": project_id, "pipeline_state": "立项"}
        if not isinstance(data, dict):
            data = {"id": project_id, "title": project_id, "pipeline_state": "立项"}
        data["forge"] = self.to_dict()
        ws.write_json(path, data)
        # 同步 .checksum.json：project.json 已变，不刷新则后续 Checkpoint.restore
        # 误报「file checksum changed」（M3l F5 发现：build 后 validate 的 restore 失败）
        try:
            from ..storage.checkpoint import Checkpoint

            Checkpoint(ws).save(project_id, data)
        except Exception:  # noqa: BLE001 - checksum 刷新失败不阻断 forge 主流程
            pass

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "interaction": self.interaction,
            "stage": self.stage,
            "blueprint_rev": self.blueprint_rev,
            "calls_used": self.calls_used,
            "started_at": self.started_at,
            "last_error": self.last_error,
        }

    def touch_stage(self, ws: Workspace, project_id: str, stage: str) -> None:
        self.stage = stage
        self.save(ws, project_id)


# ---- transcript（docs/10 §4.3）----
def append_transcript(ws: Workspace, project_id: str, event: str, **extra: Any) -> Path:
    """问答/构建留痕，每行一个事件。中断续跑与审计用。"""
    path = ws._abs(f"{project_id}/{FORGE_REL}/{TRANSCRIPT_FILE}")  # noqa: SLF001
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, **extra}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def read_transcript(ws: Workspace, project_id: str) -> list[dict]:
    path = ws._abs(f"{project_id}/{FORGE_REL}/{TRANSCRIPT_FILE}")  # noqa: SLF001
    if not path.exists():
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out
