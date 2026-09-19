"""设定条目库 + 首次交代状态机（讨论决策）。

## 问题

实测《断玉青冥》第一章直接进场景，不交代境界体系、宗门背景——worldview.json 里
设定齐全，但模型不知道"首次提及要展开"。且全量塞进 system prompt 会随章节膨胀，
触发思考型模型的预算问题。

## 机制（用户拍板的形态 + 注入方式决策）

- `bible/settings.json`：世界观按独立知识单元切块（境界体系/势力/物品背景…），
  每条带 `keywords[]` 与 `revealed: bool`（是否已交代）。
- **生成前（提及检测）**：拿当前事件文本 + 细纲做关键词匹配 → 命中且 `revealed=false`
  的条目 → 注入事件 prompt 的【待交代设定】段，注明"首次出现，须在正文自然带出"。
- **生成后（交代验证）**：扫描成稿正文关键词 → 命中即置 `revealed=true`（写回文件）；
  未命中保留 false，下一章继续注入。

注入方式选 **prompt 引导**而非代码直接插入设定文本——直接插入会写出说明书腔，
LLM 自然带出才符合文风；确定性由"交代验证"闭环保证（首次交代是硬状态，不是模型自觉）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class SettingEntry:
    id: str
    keywords: list[str] = field(default_factory=list)
    text: str = ""
    revealed: bool = False
    first_ch: int = 1

    @classmethod
    def from_dict(cls, d: dict) -> "SettingEntry":
        return cls(
            id=str(d.get("id", "")),
            keywords=[str(k) for k in (d.get("keywords") or [])],
            text=str(d.get("text", "") or ""),
            revealed=bool(d.get("revealed", False)),
            first_ch=int(d.get("first_ch", 1) or 1),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "keywords": self.keywords,
            "text": self.text,
            "revealed": self.revealed,
            "first_ch": self.first_ch,
        }


@dataclass
class SettingIndex:
    """设定条目库（bible/settings.json 的内存形态）。"""

    entries: list[SettingEntry] = field(default_factory=list)
    _path: str = ""

    # ---------------------------------------------------------------- 加载/落盘
    @classmethod
    def load(cls, ws, project_id: str) -> "SettingIndex":
        idx = cls()
        p = ws.bible_path(project_id, "settings")
        if not p or not p.exists():
            return idx
        idx._path = str(p)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return idx
        idx.entries = [SettingEntry.from_dict(d) for d in (data or []) if isinstance(d, dict)]
        return idx

    def save(self) -> None:
        """写回（交代验证后更新 revealed）。无路径（条目库不存在）时跳过。"""
        if not self._path:
            return
        from pathlib import Path

        Path(self._path).write_text(
            json.dumps([e.to_dict() for e in self.entries], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ---------------------------------------------------------------- 检测
    def hits(self, *texts: str) -> list[SettingEntry]:
        """提及检测：任一文本命中条目任一关键词 → 该条目相关。"""
        pool = "\n".join(t for t in texts if t)
        if not pool:
            return []
        return [e for e in self.entries if any(k and k in pool for k in e.keywords)]

    def pending(self, *texts: str, ch: int = 0) -> list[SettingEntry]:
        """待交代条目 = 命中 且 未交代（首次提及才注入，已交代的不再注入省 token）。"""
        out = []
        for e in self.hits(*texts):
            if e.revealed:
                continue
            if ch and e.first_ch and ch < e.first_ch:
                continue
            out.append(e)
        return out

    def verify(self, chapter_text: str) -> list[str]:
        """交代验证：扫描正文，关键词命中 → 置 revealed=true。返回新交代的条目 id。"""
        if not chapter_text:
            return []
        done = []
        for e in self.entries:
            if e.revealed:
                continue
            if any(k and k in chapter_text for k in e.keywords):
                e.revealed = True
                done.append(e.id)
        return done

    # ---------------------------------------------------------------- 注入文本
    def pending_lines(self, *texts: str, ch: int = 0, max_entries: int = 4) -> list[str]:
        """把待交代条目转成 prompt 行（每条限长，避免预算膨胀）。"""
        lines = []
        for e in self.pending(*texts, ch=ch)[:max_entries]:
            text = e.text.strip()
            if len(text) > 120:
                text = text[:117] + "…"
            lines.append(f"- {e.id}：{text}")
        return lines
