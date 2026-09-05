"""统一实体追踪：四阶段引入状态机 + 别名共指消歧 + 信息密度预算（人工审查第八批）。

## 机制（用户拍板：人物/设定/物品/地点统一；预算从松、质量优先）

- **四阶段**：unseen → mentioned → described → established。
  阶段从正文出现次数确定性推断（0 / 1–2 / 3–5 / >5 次），可解释可复现；
  主角与核心人物（bible 有完整卡）从 described 起步。
- **别名共指消歧**：从 characters（name+aliases）/ settings（term+keywords）/
  items·skills（name+aliases）构建"实体 key → 全部称呼"映射，正文扫描时
  "叶岚/叶师弟""五五开系统/系统"计入同一实体——比四阶段更基础：实体身份
  合并错了，阶段推进必然算错（实测 v5 统计 43/26 人，虚高即此因）。
- **信息密度预算**：每章首次出现实体数统计，超预算记告警（松档：日常 3 /
  群像 5 / 大比 7；用户拍板"更松一些，以质量为主"）。
- **应然/实然分离（ADR-011）**：人物卡/settings/items 是应然（bible 原文件
  不动）；引入进度是实然，独立落盘 `bible/entity_progress.json`。

## 注入差异化（接 KnowledgeBase 事件级注入）

- stage < described 且本章命中 → 注入"需展开"行（带 bible 摘要，首次交代）；
- stage == established → 相关注入只给名字行（省 token，防重复介绍啰嗦）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

STAGES = ("unseen", "mentioned", "described", "established")
_STAGE_RANK = {s: i for i, s in enumerate(STAGES)}

# 出现次数 → 阶段（确定性推断；核心人物从 described 起步见 _core_keys）
_STAGE_BY_MENTIONS = [(0, "unseen"), (3, "mentioned"), (6, "described"), (999999, "established")]
# 说明：>0 且 <3 次 → mentioned；3–5 次 → described；≥6 次 → established

# 松预算分档（用户拍板：更松一些，质量优先）
DEFAULT_BUDGET = {"default": 3, "ensemble": 5, "climax": 7}


@dataclass
class EntityProgress:
    key: str                 # 唯一键：char:xxx / setting:xxx / item:xxx / loc:xxx
    type: str                # character / setting / item / location / skill
    name: str
    aliases: list[str] = field(default_factory=list)
    stage: str = "unseen"
    first_ch: int = 0        # 正文首次出现章（0=未出现）
    last_ch: int = 0
    mentions: int = 0

    def to_dict(self) -> dict:
        return {"key": self.key, "type": self.type, "name": self.name,
                "aliases": self.aliases, "stage": self.stage,
                "first_ch": self.first_ch, "last_ch": self.last_ch,
                "mentions": self.mentions}


class EntityTracker:
    """统一实体进度层（bible 应然 + 正文实然 → entity_progress.json）。"""

    def __init__(self) -> None:
        self.entities: dict[str, EntityProgress] = {}
        self._alias_map: dict[str, str] = {}   # 别名/规范名 → key（共指消歧）
        self._core_keys: set[str] = set()      # 主角/核心人物：从 described 起步
        self._path: str = ""
        self._budget_conf: dict = dict(DEFAULT_BUDGET)
        self.deferred: list[str] = []          # 配额超限被推迟的实体 key（第九批·开篇配额）

    # ---------------------------------------------------------------- 构建/落盘
    @classmethod
    def load(cls, ws, project_id: str) -> "EntityTracker":
        """从 bible（应然）+ entity_progress.json（实然缓存）构建。"""
        t = cls()

        def _read(rel: str):
            p = ws._abs(f"{project_id}/bible/{rel}")
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

        # 人物：name + aliases（is_protagonist / 有完整 power 卡 → 核心，described 起步）
        for c in (_read("characters.json") or []):
            if not isinstance(c, dict) or not c.get("id"):
                continue
            names = [str(c["name"])] if c.get("name") else []
            names += [str(a) for a in (c.get("aliases") or [])]
            t._register(c["id"], "character", names)
            if c.get("is_protagonist") or ((c.get("power") or {}).get("level") and c.get("core_traits")):
                t._core_keys.add(c["id"])
        # 设定：term + keywords
        for s in (_read("settings.json") or []):
            if not isinstance(s, dict) or not s.get("id"):
                continue
            names = [str(s.get("term") or s["id"])]
            names += [str(k) for k in (s.get("keywords") or [])]
            t._register(s["id"], "setting", names)
        # 物品/功法：name + aliases
        for rel, typ in (("items.json", "item"), ("skills.json", "skill")):
            for e in (_read(rel) or []):
                if not isinstance(e, dict) or not e.get("id"):
                    continue
                names = [str(e["name"])] if e.get("name") else []
                names += [str(a) for a in (e.get("aliases") or [])]
                t._register(e["id"], typ, names)
        # 地点
        for loc in (_read("locations.json") or []):
            if not isinstance(loc, dict) or not loc.get("id"):
                continue
            names = [str(loc["name"])] if loc.get("name") else []
            names += [str(a) for a in (loc.get("aliases") or [])]
            t._register(loc["id"], "location", names)

        # 预算配置（worldview.entity_budget 可覆盖；缺省松档）
        wv = _read("worldview.json") or {}
        if isinstance(wv.get("entity_budget"), dict):
            t._budget_conf.update({k: int(v) for k, v in wv["entity_budget"].items()})

        # 实然进度（缓存存在则合并：stage/mentions 以缓存为准）
        p = ws._abs(f"{project_id}/bible/entity_progress.json")
        if p.exists():
            t._path = str(p)
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                # 新格式 {"entities": [...], "deferred": [...]}；旧格式为裸 list
                if isinstance(raw, dict):
                    entries = raw.get("entities") or []
                    t.deferred = [str(k) for k in (raw.get("deferred") or [])]
                else:
                    entries = raw or []
                for d in entries:
                    if not isinstance(d, dict) or not d.get("key"):
                        continue
                    old = t.entities.get(d["key"])
                    if old:
                        old.stage = d.get("stage", old.stage)
                        old.first_ch = int(d.get("first_ch", 0) or 0)
                        old.last_ch = int(d.get("last_ch", 0) or 0)
                        old.mentions = int(d.get("mentions", 0) or 0)
            except (ValueError, OSError):
                pass
        else:
            t._path = str(p)
        return t

    def _register(self, key: str, typ: str, names: list[str]) -> None:
        e = EntityProgress(key=key, type=typ, name=names[0] if names else key,
                           aliases=[n for n in names[1:] if n])
        self.entities[key] = e
        for n in names:
            if n:
                self._alias_map.setdefault(n, key)

    def save(self) -> None:
        if not self._path:
            return
        Path(self._path).write_text(
            json.dumps({"entities": [e.to_dict() for e in self.entities.values()],
                        "deferred": self.deferred},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")

    # ---------------------------------------------------------------- 正文扫描（实然更新）
    def _stage_for(self, key: str, mentions: int) -> str:
        if mentions <= 0:
            return "unseen"
        stage = "mentioned"
        if mentions >= 3:
            stage = "described"
        if mentions >= 6:
            stage = "established"
        if key in self._core_keys and stage == "mentioned":
            stage = "described"  # 核心人物首次出场即带完整描写
        return stage

    def update_from_chapter(self, chapter_text: str, vol: int, ch: int) -> dict:
        """扫正文更新实体进度（别名共指消歧）。返回 {new, stage_up, mentions} 供日志。"""
        new_keys: list[str] = []
        stage_up: list[str] = []
        if not chapter_text:
            return {"new": new_keys, "stage_up": stage_up}
        for key, e in self.entities.items():
            count = 0
            for alias in [e.name, *e.aliases]:
                if alias:
                    count += chapter_text.count(alias)
            if count <= 0:
                continue
            if e.first_ch == 0:
                e.first_ch, e.last_ch = ch, ch
                new_keys.append(key)
            else:
                e.last_ch = max(e.last_ch, ch)
            old_stage = e.stage
            e.mentions += count
            e.stage = self._stage_for(key, e.mentions)
            if _STAGE_RANK[e.stage] > _STAGE_RANK[old_stage]:
                stage_up.append(f"{e.name}({old_stage}→{e.stage})")
        return {"new": new_keys, "stage_up": stage_up}

    # ---------------------------------------------------------------- 预算（松档，用户拍板）
    def budget_check(self, new_keys: list[str], chapter_type: str = "default",
                     *, quota: int | None = None,
                     defer_over: bool = False) -> list[str]:
        """本章新实体数超预算 → 告警行（记录不阻断）。

        第九批（开篇配额）：`quota` 显式给定本章配额（PhasePolicy.opening_quota）；
        `defer_over=True` 时超出配额的部分记入 `self.deferred`（持久化，下一章
        开篇期优先注入介绍——"推迟引入"从口号变成机制）。
        """
        cap = quota if quota is not None else self._budget_conf.get(
            chapter_type, self._budget_conf["default"])
        if len(new_keys) <= cap:
            return []
        over = [self.entities[k].name for k in new_keys if k in self.entities]
        if defer_over:
            for k in new_keys[cap:]:
                if k not in self.deferred:
                    self.deferred.append(k)
        return [f"新实体 {len(new_keys)} 个超预算（上限 {cap}）：{over}——"
                f"超出部分{'已推迟到后续章节优先引入' if defer_over else '可考虑推迟引入'}"]

    def deferred_names(self) -> list[str]:
        """被推迟实体的名字行（开篇期注入：优先介绍这批，防读者认知断档）。"""
        out = []
        for k in self.deferred:
            e = self.entities.get(k)
            if e:
                out.append(e.name)
        return out

    def clear_deferred(self, introduced_keys: list[str]) -> None:
        """本章已实际介绍（正文命中）的推迟实体出队。"""
        hit = set(introduced_keys) & set(self.deferred)
        self.deferred = [k for k in self.deferred if k not in hit]

    # ---------------------------------------------------------------- 注入（差异化）
    def resolve(self, text: str) -> list[EntityProgress]:
        """文本命中的实体（别名归并；去重）。"""
        seen: dict[str, EntityProgress] = {}
        for alias, key in self._alias_map.items():
            if alias in text and key in self.entities:
                seen.setdefault(key, self.entities[key])
        return list(seen.values())

    def needs_expansion(self, text: str, ch: int = 0) -> list[EntityProgress]:
        """命中且 stage 不足 described → 需要"展开介绍"的实体。"""
        out = []
        for e in self.resolve(text):
            if _STAGE_RANK[e.stage] < _STAGE_RANK["described"]:
                if ch and e.first_ch and ch < e.first_ch:
                    continue  # 尚未到出场章
                out.append(e)
        return out

    def established_names(self, text: str) -> list[str]:
        """命中且已 established → 只给名字（省 token，防重复介绍）。"""
        return [e.name for e in self.resolve(text) if e.stage == "established"]

    # ---------------------------------------------------------------- 查询
    def stage_of(self, key: str) -> str:
        e = self.entities.get(key)
        return e.stage if e else "unseen"

    def is_core(self, key: str) -> bool:
        """是否主角/核心人物（开篇交代由 opening_rule 负责，首登场检查跳过）。"""
        return key in self._core_keys

    def dormant_since(self, ch: int, min_gap: int = 5) -> list[str]:
        """超过 min_gap 章未出场且已 described 的实体（供检查员/评阅）。"""
        out = []
        for e in self.entities.values():
            if _STAGE_RANK[e.stage] >= _STAGE_RANK["described"] and e.last_ch \
                    and ch - e.last_ch >= min_gap:
                out.append(f"{e.name}（{e.last_ch} 章后未出场）")
        return out
