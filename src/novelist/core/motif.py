"""行文母题账本（批次二·方案 D，用户 2026-09-05 拍板）。

针对真机实证的"母题复用"重复：蓝图事件措辞不同（Jaccard<0.5 放行）但标志性
动作/意象反复出现（"新闻推送"单章 3 次、"指尖轻划"跨章复用）。本账本做两件事：

1. 机械提取：从成稿正文抽取含动作/工具线索词的短句（母题句），按章累计；
2. 生成前注入：把已用 ≥2 次的母题作为【母题禁令】注入后续章 prompt。

账本落 ``workspace/forge/motif_ledger.json``（ADR-016：文件为持久事实源）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# 母题线索词：标志性动作 + 高辨识度道具/意象（宁可多收，注入侧再按频次过滤）
MOTIF_CUE_RE = re.compile(
    "指尖|抬手|挥手|虚画|轻弹|轻划|一划|掐诀|剑指|并拢|屈指|"
    "手机|屏幕|推送|电话|短信|消息|键盘|显示器|"
    "神识|阵法|结界|符箓|灵压|领域")

LEDGER_REL = "workspace/forge/motif_ledger.json"


_PUNCT_RE = re.compile('[\\s，,。！！?？；:：…「」『』（）()【】、·—“”‘\'"“-]+')


def _norm(sentence: str) -> str:
    return _PUNCT_RE.sub("", sentence)


def extract_motifs(text: str, *, max_len: int = 40, limit: int = 12) -> list[str]:
    """抽取正文中的母题句：含线索词且长度 ≤ max_len 的句子，归一去重。"""
    if not text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for sent in re.split(r"[。！？\n]+", text):
        s = sent.strip()
        if not s or len(s) > max_len or not MOTIF_CUE_RE.search(s):
            continue
        key = _norm(s)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(s)
        if len(out) >= limit:
            break
    return out


class MotifLedger:
    """母题账本：entries = [{motif, count, chapters:[int]}]。"""

    def __init__(self, entries: list[dict] | None = None) -> None:
        self.entries: list[dict] = entries or []

    # ---- 持久化 ----
    @classmethod
    def load(cls, ws, project_id: str) -> "MotifLedger":
        try:
            p = Path(ws._abs(f"{project_id}/{LEDGER_REL}"))  # noqa: SLF001
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                return cls(data.get("entries") or [])
        except Exception:  # noqa: BLE001 - 损坏账本按空处理
            pass
        return cls()

    def save(self, ws, project_id: str) -> None:
        try:
            ws.write_json(ws._abs(f"{project_id}/{LEDGER_REL}"),  # noqa: SLF001
                          {"entries": self.entries})
        except Exception:  # noqa: BLE001 - 账本写失败不阻断生成
            pass

    # ---- 记账 / 查询 ----
    def remove_chapter(self, ch: int) -> int:
        """清掉某章的母题记账（重跑幂等，2026-09-05）。

        同章重生成时以最新正文为准：先清该章旧账再入新账，防 count 翻倍、
        禁令虚增触发。legacy 条目（无 by_ch）按"每章记 1 次"归一 count——
        顺带治愈历史重跑造成的 count 虚高。返回受影响条目数。
        """
        touched = 0
        for e in self.entries:
            chs = e.get("chapters")
            by = e.get("by_ch")
            if not (isinstance(chs, list) and ch in chs) and not (isinstance(by, dict) and str(ch) in by):
                continue
            touched += 1
            if isinstance(chs, list):
                e["chapters"] = [c for c in chs if c != ch]
            if isinstance(e.get("by_ch"), dict) and e["by_ch"]:
                e["by_ch"].pop(str(ch), None)
                e["count"] = sum(int(v) for v in e["by_ch"].values())
            elif isinstance(e.get("chapters"), list):
                e["count"] = len(e["chapters"])
        if touched:
            self.entries = [e for e in self.entries if int(e.get("count") or 0) > 0]
        return touched

    def add_text(self, text: str, ch: int) -> int:
        """从正文抽取母题并入账。返回新增（含合并）条数。

        累计语义：同一章多次调用会累计（历史行为，测试与外部调用依赖）。
        **章节重跑幂等由调用方保证**：orchestrator 在 add_text 前先调
        `remove_chapter(ch)` 清旧账（I2 修复，2026-09-05）——这样单章内
        多段入账仍能累计，重跑又不会翻倍。
        """
        added = 0
        for m in extract_motifs(text):
            key = _norm(m)
            ent = next((e for e in self.entries if e.get("motif") == key), None)
            if ent is None:
                self.entries.append({"motif": key, "count": 1, "chapters": [ch],
                                     "by_ch": {str(ch): 1}})
            else:
                by = ent.setdefault("by_ch", {})
                if isinstance(by, dict):
                    by[str(ch)] = int(by.get(str(ch), 0)) + 1
                ent["count"] = int(ent.get("count") or 0) + 1
                if ch not in (ent.get("chapters") or []):
                    ent.setdefault("chapters", []).append(ch)
            added += 1
        return added

    def ban_lines(self, *, min_count: int = 2, limit: int = 8) -> list[str]:
        """生成禁令清单：已用 ≥min_count 次的母题，频次降序。"""
        hot = [e for e in self.entries if int(e.get("count") or 0) >= min_count]
        hot.sort(key=lambda e: -int(e.get("count") or 0))
        lines = []
        for e in hot[:limit]:
            chs = "、".join(str(c) for c in (e.get("chapters") or []))
            lines.append(f"「{e.get('motif')}」已用 {e.get('count')} 次（第 {chs} 章）")
        return lines
