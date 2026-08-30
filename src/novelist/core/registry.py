"""实体注册表（游戏式资产登记，讨论决策）。

## 为什么需要注册表

实测（《无双开局》）暴露：同一部功法《太上忘情录》在正文出现「圆满/残篇/残卷」
三个名字，worldstate.items 只做字符串追加，同物异名无法去重——物品是唯一没有
"唯一身份"的实体类型（人名有 characters.json、地名有 locations.json）。

注册表给物品/功法唯一 id + 规范名 + 别名表，让 worldstate 存规范名/id 而非自由
字符串；编纂员抽取"获得/失去"时经别名表归一化。

## 文件

- `bible/items.json`  物品注册表（可持有实体：丹药/灵石/法器）
- `bible/skills.json` 功法注册表（可学习实体：功法/术法）——与物品分离（讨论决策）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class RegistryEntry:
    id: str
    name: str
    type: str = "other"
    aliases: list[str] = field(default_factory=list)
    state: str = ""
    note: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "RegistryEntry":
        return cls(
            id=str(d.get("id", "")),
            name=str(d.get("name", "")),
            type=str(d.get("type", "other")),
            aliases=[str(a) for a in (d.get("aliases") or [])],
            state=str(d.get("state", "") or ""),
            note=str(d.get("note", "") or ""),
        )


@dataclass
class Registry:
    """物品 + 功法双注册表（对应 items.json / skills.json）。"""

    items: dict[str, RegistryEntry] = field(default_factory=dict)
    skills: dict[str, RegistryEntry] = field(default_factory=dict)

    # ---------------------------------------------------------------- 加载
    @classmethod
    def load(cls, ws, project_id: str) -> "Registry":
        reg = cls()
        for rel, target in (("bible/items.json", reg.items),
                            ("bible/skills.json", reg.skills)):
            p = ws._abs(f"{project_id}/{rel}")
            if not p or not p.exists():
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            for d in data or []:
                if isinstance(d, dict) and d.get("id"):
                    target[d["id"]] = RegistryEntry.from_dict(d)
        return reg

    # ---------------------------------------------------------------- 查询
    def all_entries(self) -> list[RegistryEntry]:
        return list(self.items.values()) + list(self.skills.values())

    def find_by_name(self, name: str) -> RegistryEntry | None:
        """按规范名或别名查找条目（先精确名，再别名）。"""
        if not name:
            return None
        name = _strip(name)
        for e in self.all_entries():
            if e.name == name:
                return e
        for e in self.all_entries():
            if name in [a for a in e.aliases] or name in _expand(e):
                return e
        return None

    def canonical(self, name: str) -> str:
        """别名归一化：任意叫法 → 规范名（找不到原样返回）。"""
        e = self.find_by_name(name)
        return e.name if e else name


def _strip(s: str) -> str:
    """去掉书名号/括号注释/空格，便于匹配。"""
    for ch in "《》「」『』[]（）()「」、，。 ":
        s = s.replace(ch, "")
    return s.strip()


def _expand(e: RegistryEntry) -> list[str]:
    """扩展匹配：别名 + 规范名去掉书名号后的形态。"""
    out = [_strip(e.name)]
    if e.state:
        out.append(_strip(f"{e.name}（{e.state}）"))
        out.append(_strip(f"{e.name}{e.state}"))
    return out
