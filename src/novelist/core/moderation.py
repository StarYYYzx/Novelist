"""审核拦截降级与敏感词预检（docs/04 §5.10 / docs/07 §2.6，ADR-015）。

上游预检降低厂商侧拦截概率；识别拦截后走改写→切换→人工链路（编排层负责状态）。

## B-09：默认词表原本是空的

`ModerationPrechecker(banned=None)` 得到一张**空表**，`scan()` 恒返回空列表——
ADR-015 的"上游预检降低审核命中"形同虚设。本模块补上：

- `DEFAULT_BANNED_WORDS`：一份**起步词表**（按类别分组，覆盖违禁品/赌博/极端暴力/违规导流）。
  ⚠️ 这只是起步清单，**不构成本地合规词表**；生产环境必须通过 `banned_words_file`
  加载完整词表（或对接专业审核服务）。
- `load_banned_words(path)`：从文本文件（每行一词，`#` 开头为注释）或 JSON 列表加载。
- `ModerationPrechecker.from_workspace()`：按工作区约定自动找词表文件。

词表文件查找顺序：
1. 显式传入的 `banned_words_file`
2. `<project>/bible/moderation.json`（`{"banned_words": [...]}`）
3. 环境变量 `NOVELIST_BANNED_WORDS`
4. 兜底用内置 `DEFAULT_BANNED_WORDS`
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# 起步词表（分组织，便于按需裁剪与扩展）
DEFAULT_BANNED_WORDS: tuple[str, ...] = (
    # 违禁品
    "海洛因", "冰毒", "甲基苯丙胺", "摇头丸", "大麻", "可卡因", "鸦片", "吗啡", "罂粟",
    # 赌博
    "六合彩", "博彩公司", "境外赌博", "网络赌博",
    # 极端暴力与危险行为
    "制造炸药", "自制枪支", "投毒方法", "自杀教程",
    # 违规导流
    "加微信", "扫码进群", "私聊交易", "点击链接领取",
)

# 命中后是否直接阻断（True）还是仅告警（False）。起步阶段默认告警，避免误杀创作。
DEFAULT_BLOCK_ON_HIT = False


def load_banned_words(path: str | os.PathLike[str] | None) -> list[str]:
    """从文件加载敏感词：`.txt`（每行一词，`#` 注释）或 `.json`（数组 / {banned_words: []}）。"""
    if not path:
        return []
    p = Path(path)
    if not p.exists():
        return []
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:  # pragma: no cover
        return []
    if p.suffix.lower() == ".json":
        try:
            data = json.loads(raw)
        except ValueError:
            return []
        if isinstance(data, list):
            return [str(w) for w in data if str(w).strip()]
        if isinstance(data, dict):
            return [str(w) for w in (data.get("banned_words") or []) if str(w).strip()]
        return []
    out = []
    for line in raw.splitlines():
        w = line.strip()
        if w and not w.startswith("#"):
            out.append(w)
    return out


class ModerationPrechecker:
    """本地敏感词预检（可插拔词表，ADR-015 上游预检）。"""

    def __init__(self, banned: list[str] | None = None, *, block_on_hit: bool = DEFAULT_BLOCK_ON_HIT) -> None:
        # None 表示"未指定"→ 用内置起步词表；显式传 [] 才表示"不启用"
        self._banned = list(DEFAULT_BANNED_WORDS) if banned is None else list(banned)
        self.block_on_hit = block_on_hit

    @property
    def banned(self) -> list[str]:
        return list(self._banned)

    @classmethod
    def from_workspace(cls, ws, project_id: str, *, banned_words_file: str | None = None) -> "ModerationPrechecker":
        """按工作区约定加载词表（见模块 docstring 的查找顺序）。"""
        words: list[str] = []
        src = banned_words_file
        if not src:
            p = ws.bible_path(project_id, "moderation")
            if p.exists():
                words = load_banned_words(p)
                src = str(p)
        if not words:
            env = os.getenv("NOVELIST_BANNED_WORDS")
            if env:
                words = load_banned_words(env)
        if not words:
            words = list(DEFAULT_BANNED_WORDS)
        return cls(banned=words)

    def scan(self, text: str) -> list[str]:
        """返回命中的敏感词列表；空列表表示通过预检。"""
        return [w for w in self._banned if w and w in text]

    def predicate_hit(self, text: str) -> bool:
        return bool(self.scan(text))

    def check(self, text: str) -> dict:
        """结构化结果，便于编排层决定是否改写/切换/提请人工（docs/04 §5.10）。"""
        hits = self.scan(text)
        return {
            "passed": not hits,
            "hits": hits,
            "block": bool(hits) and self.block_on_hit,
            "action": "none" if not hits else ("block" if self.block_on_hit else "warn"),
        }
