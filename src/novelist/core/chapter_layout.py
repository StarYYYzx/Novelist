"""事件流与切章（**事件先行、章节后置**，2026-09-19 拍板）。

## 为什么

此前是"章节先行"：forge 规划期为每一章产细纲（`key_events` 恰好 2–3 个 + hook + tension），
章边界与事件归属在动笔前锁死。真机实证两个后果：

1. **塌缩**：卷一共 100 章逐章生成，每章只看得到前一章 gist → 局部延续、全局失向
   （proj-20260919164352 卷一 ch12 起 72 章只有主角一人出场）；
2. **与文学规律相悖**：章节是**阅读排版单位**（场景结束/冲突爆点/悬念/时间跳转/视角切换处切），
   不是故事逻辑单位——"不要用章节的划分绑架主线阶段的完成"。

本模块改为：规划期只产出**卷级连续事件流**（事件带场景/视角/时间跨度/预估字数/所属节），
正文生成时按"目标字数 + 切点线索"**确定性切章**，章细纲文件成为**切片派生物**
（路径与 front-matter 格式与旧版一致 → 审校/publish/统计等下游零改动）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

EVENTS_REL = "outline/events/vol-{vol}.json"

# 切章阈值（字数）：达到 min 才允许按切点切开；达到 max 无条件切
DEFAULT_TARGET_WORDS = 2400
DEFAULT_MIN_WORDS = 1500
DEFAULT_MAX_WORDS = 4200
# 事件未给 est_words 时的兜底估算（按描述字数）
_FALLBACK_WORDS_PER_CHAR = 3.2


def events_rel(vol: int) -> str:
    return EVENTS_REL.format(vol=int(vol))


def events_path(ws, project_id: str, vol: int) -> Path:
    return ws._abs(f"{project_id}/{events_rel(vol)}")  # noqa: SLF001 - 与其它访问器同口径


def has_stream(ws, project_id: str, vol: int) -> bool:
    try:
        return events_path(ws, project_id, vol).exists()
    except Exception:  # noqa: BLE001 - 路径异常一律视为无流（降级）
        return False


def load_events(ws, project_id: str, vol: int) -> list[dict]:
    p = events_path(ws, project_id, vol)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("events")
    return [e for e in (data or []) if isinstance(e, dict) and str(e.get("desc") or "").strip()]


def save_events(ws, project_id: str, vol: int, events: list[dict]) -> None:
    p = events_path(ws, project_id, vol)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"vol": int(vol), "count": len(events), "events": events}
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    import os as _os

    _os.replace(tmp, p)


def est_words(ev: dict) -> int:
    """事件预估字数：显式 est_words 优先，否则按描述长度估算。"""
    try:
        n = int(ev.get("est_words") or 0)
    except (TypeError, ValueError):
        n = 0
    if n > 0:
        return n
    return max(200, int(len(str(ev.get("desc") or "")) * _FALLBACK_WORDS_PER_CHAR))


def _norm_key(ev: dict, field: str) -> str:
    return str(ev.get(field) or "").strip()


def slice_chapters(events: list[dict], *, target_words: int = DEFAULT_TARGET_WORDS,
                   min_words: int = DEFAULT_MIN_WORDS,
                   max_words: int = DEFAULT_MAX_WORDS) -> list[dict]:
    """把连续事件流切成章（确定性，零 LLM）。

    切点线索（累计字数 ≥ min_words 后才有资格切）：
    `climax` 高潮事件之后 / 下一条 `days > 0`（时间跳） / `pov` 变化 / `scene` 变化 /
    累计 ≥ target_words（软目标）/ 累计 ≥ max_words（硬上限）/ 流结束。

    返回 `[{"ch": 1, "start": 0, "end": 3, "events": [...], "est_words": n, "break": "scene-change"}]`。
    """
    out: list[dict] = []
    if not events:
        return out
    start, acc = 0, 0
    for i, ev in enumerate(events):
        acc += est_words(ev)
        nxt = events[i + 1] if i + 1 < len(events) else None
        reason = ""
        if acc >= max_words:
            reason = "max-words"
        elif nxt is None:
            reason = "stream-end"
        elif acc >= min_words:
            if ev.get("climax"):
                reason = "climax"
            elif int(nxt.get("days") or 0) > 0:
                reason = "time-skip"
            elif _norm_key(nxt, "pov") and _norm_key(nxt, "pov") != _norm_key(ev, "pov"):
                reason = "pov-change"
            elif _norm_key(nxt, "scene") and _norm_key(nxt, "scene") != _norm_key(ev, "scene"):
                reason = "scene-change"
            elif acc >= target_words:
                reason = "target-words"
        if reason:
            chunk = events[start:i + 1]
            out.append({"ch": len(out) + 1, "start": start, "end": i + 1,
                        "events": chunk,
                        "est_words": sum(est_words(x) for x in chunk),
                        "break": reason})
            start, acc = i + 1, 0
    return out


def chapter_slice(events: list[dict], ch: int, **kw) -> dict | None:
    """取第 ch 章的切片（1 基）；越界返回 None。"""
    slices = slice_chapters(events, **kw)
    return slices[ch - 1] if 1 <= ch <= len(slices) else None


def chapter_count(events: list[dict], **kw) -> int:
    return len(slice_chapters(events, **kw))


def _join_text(items, sep="；", limit: int = 0) -> str:
    vals = [str(x) for x in (items or []) if str(x).strip()]
    if limit:
        vals = vals[:limit]
    return sep.join(vals)


def materialize_outline(ws, project_id: str, vol: int, sl: dict) -> Path:
    """把事件切片物化成**章细纲文件**（路径/格式与旧版一致 → 下游零改动）。

    - `key_events` = 切片内事件描述（正文事件循环直接迭代）；
    - `tension`/`hook` = 首/末事件自带字段（无则留空，由正文期生成）；
    - `characters`/`threads_involved`/`lines_present` = 该切片事件的并集；
    - `after_days` = 切片内 days 之和；`title` 留占位（成稿后由延迟拟题覆盖）。
    """
    ch = int(sl["ch"])
    evs = sl.get("events") or []
    chars: list[str] = []
    threads: list[str] = []
    lines: list[dict] = []
    for ev in evs:
        for c in ev.get("characters") or []:
            if c not in chars:
                chars.append(str(c))
        for t in ev.get("threads_involved") or []:
            if t not in threads:
                threads.append(str(t))
        beads = ev.get("beads") or {}
        for x in (beads.get("lines") if isinstance(beads, dict) else None) or []:
            if isinstance(x, dict) and x.get("id"):
                lines.append({"id": str(x["id"]),
                              "action": str(x.get("action") or "advance"),
                              "note": str(x.get("note") or "")[:80]})
    gist = {
        "title": "",
        "pov": str(evs[0].get("pov") or "") if evs else "",
        "key_events": [str(ev.get("desc") or "") for ev in evs],
        "turns": [str(ev.get("turn") or "") for ev in evs if ev.get("turn")],
        "tension": str((evs[0].get("tension") if evs else "") or ""),
        "hook": str((evs[-1].get("hook") if evs else "") or ""),
        "characters": chars,
        "threads_involved": threads,
        "lines_present": lines,
        "after_days": sum(int(ev.get("days") or 0) for ev in evs),
        "beats": [],
    }
    # 复用 forge 的渲染器（唯一实现，避免格式漂移；函数内延迟导入避免层次倒置）。
    # `char_names` 用于行内「出场人物:」——必须是**名字**（id → name 解析），
    # 否则 parse_cast_decl 读出来是 char:xxx（正文期 cast 展示与测试都按名字口径）。
    from ..forge.nodes import render_gist_md

    names = list(chars)
    try:
        from ..forge.state import Blueprint as _BP

        by_id = {str(c.get("id")): str(c.get("name") or "") for c in _BP.load(ws, project_id).section("characters")}
        names = [by_id.get(c, "") or c for c in chars]
    except Exception:  # noqa: BLE001 - 蓝图不可读时退回 id（不阻断物化）
        names = list(chars)
    md = render_gist_md(gist, vol, ch, names)
    p = ws.outline_chapter_path(project_id, vol, ch)
    p.parent.mkdir(parents=True, exist_ok=True)
    ws.write_text(p, md)
    return p


def events_from_outlines(ws, project_id: str, vol: int) -> list[dict]:
    """迁移：把既有**章细纲**摊平成事件流（存量项目立即可用，无需重跑 build）。

    章序即事件序（`after_days` 保留为该章首事件的 days）；场景/视角从细纲字段取，
    无则留空（切章器见到空字段就不会据此切——退化为按字数切）。
    """
    from ..core.bible import parse_gist  # noqa: PLC0415 - 避免顶层循环

    events: list[dict] = []
    ch = 1
    while True:
        try:
            g = parse_gist(ws, project_id, vol, ch)
        except Exception:  # noqa: BLE001 - 读取异常视为流结束
            g = None
        if not isinstance(g, dict) or not g.get("key_events"):
            break
        ke = [str(x) for x in g.get("key_events") or []]
        for i, desc in enumerate(ke):
            events.append({
                "id": f"ev:{vol}-{ch}-{i + 1}",
                "desc": desc,
                "scene": "",
                "pov": str(g.get("pov") or ""),
                "days": int(g.get("after_days") or 0) if i == 0 else 0,
                "est_words": 0,
                "climax": bool(i == len(ke) - 1),
                "source_ch": ch,
            })
        ch += 1
        if ch > 2000:  # 防御：异常目录结构下不至死循环
            break
    return events


def summary(events: list[dict], **kw) -> str:
    """给 CLI/console 的单行摘要（事件数 / 切出章数 / 每章均字数）。"""
    if not events:
        return "（无事件流）"
    slices = slice_chapters(events, **kw)
    total = sum(est_words(e) for e in events)
    avg = int(total / len(slices)) if slices else 0
    return (f"事件 {len(events)} 条 → 切出 {len(slices)} 章（预估均 {avg} 字/章，"
            f"总约 {total} 字）")


def event_counts_by_break(events: list[dict], **kw) -> dict[str, int]:
    """切章理由分布（观测用：切点是否主要来自场景/时间/视角而非字数上限）。"""
    out: dict[str, int] = {}
    for sl in slice_chapters(events, **kw):
        out[sl["break"]] = out.get(sl["break"], 0) + 1
    return out


def iter_stream(events: list[dict]) -> Any:  # pragma: no cover - 便捷工具
    for i, ev in enumerate(events):
        yield i, ev
