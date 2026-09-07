"""草稿溯源（ADR-030，M3z 批次 B，F2.6）。

每章草稿落盘时，并行落一份 `<vol>-<ch>.src.json` 源清单快照，记录该草稿生成时
依赖了哪些源——人物卡（cast）、线索账本、记忆基线（近期实然事件 + 记忆碎片规模）、
世界规则、血统工具（provider/model），以及装配出的 prompt 指纹。

只读工作区事实源 + 确定性重算 cast/prompt（零额外 LLM），写入走 Workspace 原子写。
清单是"生成时依赖快照"，不是实时可赎回引用：修订归因（ADR-031）据此比对草稿
改动位置与所依赖源，判断该回写哪份圣经/记忆。
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from .context import build_chapter_context


def draft_sources_path(ws, project_id: str, vol: int, ch: int):
    """源清单路径：`drafts/chapters/<vol>-<ch>.src.json`（与草稿 `<vol>-<ch>.md` 同目录）。"""
    d = ws.draft_path(project_id, vol, ch)
    return d.with_name(f"{vol}-{ch}.src.json")


def _read_json(path, default):
    if not path or not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _worldview_signals(ws, project_id: str) -> tuple[list[dict], int]:
    """世界规则 + 力量体系层数（确定性读取，缺文件给空）。"""
    wv = _read_json(ws.bible_path(project_id, "worldview"), {}) or {}
    if not isinstance(wv, dict):
        return [], 0
    rules = wv.get("rules") or []
    levels = ((wv.get("power_system") or {}).get("levels")) or []
    world_rules = []
    for r in rules[:40]:
        if isinstance(r, dict):
            world_rules.append({
                "id": str(r.get("id") or r.get("name") or "")[:80],
                "text": str(r.get("text") or r.get("desc") or r.get("name") or "")[:120],
            })
        else:  # 现实格式：rules 多为字符串铁律
            world_rules.append({"id": str(r)[:80], "text": str(r)[:120]})
    return world_rules, len(levels) if isinstance(levels, list) else 0


def _plot_thread_signals(ws, project_id: str) -> list[dict]:
    pt = _read_json(ws.bible_path(project_id, "plot_threads"), []) or []
    out = []
    if isinstance(pt, list):
        for t in pt[:40]:
            if isinstance(t, dict) and t.get("id"):
                out.append({
                    "id": str(t["id"]),
                    "name": str(t.get("name") or t.get("title") or "")[:80],
                    "status": str(t.get("status") or ""),
                })
    return out


def _settings_signals(ws, project_id: str) -> list[str]:
    st = _read_json(ws.bible_path(project_id, "settings"), []) or []
    return [str(s.get("term") or s.get("name") or "?")[:60]
            for s in st[:40] if isinstance(s, dict)]


def _memory_signals(ws, project_id: str, vol: int, ch: int) -> dict:
    """记忆基线：截至本卷本章的近期实然事件 + 记忆碎片规模（均零 LLM）。"""
    events = []
    try:
        from .memory import query_recent_actual_events

        for e in query_recent_actual_events(ws, project_id, limit=8):
            events.append({
                "vol": e.get("vol"), "ch": e.get("ch"),
                "summary": str(e.get("summary") or "")[:100],
                "kind": str(e.get("kind") or "event"),
                "participants": (e.get("participants") or [])[:6],
            })
    except Exception:  # noqa: BLE001 - 记忆基线缺失不影响溯源
        events = []
    idx = _read_json(ws.fragment_index_path(project_id), {}) or {}
    fragments = (idx.get("fragments") or []) if isinstance(idx, dict) else []
    frag_count = len(fragments) if isinstance(fragments, list) else 0
    ch_refs = 0
    if isinstance(fragments, list):
        for f in fragments:
            if not isinstance(f, dict):
                continue
            src = f.get("source") if isinstance(f.get("source"), dict) else {}
            if src.get("vol") == vol and src.get("ch") == ch:
                ch_refs += 1
    return {"recent_events": events, "fragment_count": frag_count,
            "this_chapter_refs": ch_refs}


def _prompt_fingerprint(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        if p:
            h.update(p.encode("utf-8"))
    return "sha256:" + h.hexdigest()


def _provider_signals(provider, mode: str) -> dict:
    """从 provider 上尽力提取血统（name/model）；拿不到就给空串，不抛。"""
    name = getattr(provider, "name", None) or ""
    if not name:
        inner = getattr(provider, "_inner", None)
        name = getattr(inner, "name", None) or getattr(inner, "model", None) or ""
    model = getattr(provider, "model", None) or ""
    if not model:
        inner = getattr(provider, "_inner", None)
        model = getattr(inner, "model", None) or ""
    return {"mode": mode, "provider": str(name)[:60], "model": str(model)[:80]}


def build_source_list(ws, project_id: str, vol: int, ch: int, *,
                      content: str = "", system_prompt: str = "",
                      goal: str = "", provider=None, mode: str = "tool") -> dict:
    """构建一份源清单快照（只读事实源 + 确定性重算 cast，零 LLM）。"""
    ctx = None
    cast = []
    try:
        ctx = build_chapter_context(ws, project_id, vol, ch)
        cast = [{"id": c.get("id"), "name": c.get("name")} for c in ctx.cast]
    except Exception:  # noqa: BLE001 - 上下文不可重算时不阻断溯源，cast 留空
        cast = []
    world_rules, power_levels = _worldview_signals(ws, project_id)
    return {
        "version": 1,
        "chapter": f"{vol}-{ch}",
        "vol": int(vol), "ch": int(ch),
        "generated_at": round(time.time(), 3),
        "content_chars": len(content or ""),
        **(_provider_signals(provider, mode) if provider is not None else {"mode": mode}),
        "prompt_fingerprint": _prompt_fingerprint(
            system_prompt or (ctx.system_prompt if ctx else ""),
            goal or (ctx.user_goal if ctx else ""),
            content),
        "characters": cast,
        "plot_threads": _plot_thread_signals(ws, project_id),
        "world_rules": world_rules,
        "power_system_levels": power_levels,
        "settings": _settings_signals(ws, project_id),
        "memory": _memory_signals(ws, project_id, vol, ch),
    }


def write_source_list(ws, project_id: str, vol: int, ch: int, src: dict) -> None:
    """原子落盘源清单（Workspace 原子写；父目录自动建）。"""
    ws.write_json(draft_sources_path(ws, project_id, vol, ch), src)


def read_source_list(ws, project_id: str, vol: int, ch: int) -> dict | None:
    """读回源清单；不存在返回 None（草稿可能早于溯源功能生成）。"""
    p = draft_sources_path(ws, project_id, vol, ch)
    if not p.exists():
        return None
    return _read_json(p, None)


def render_source_report(src: dict | None) -> str:
    """把源清单渲染成控制台可读文本（cli `draft show` 用）。"""
    if not src:
        return "（该章无源清单：草稿早于溯源功能生成，或生成失败）"
    L: list[str] = []
    L.append(f"草稿 {src.get('chapter')} · 源清单 v{src.get('version')}")
    L.append(f"生成于 {src.get('generated_at')} · 长度 {src.get('content_chars')} 字符")
    L.append(f"血统：mode={src.get('mode')} provider={src.get('provider') or '-'} "
             f"model={src.get('model') or '-'}")
    L.append(f"prompt 指纹：{src.get('prompt_fingerprint')}")
    chars = src.get("characters") or []
    L.append(f"人物卡（{len(chars)}）: "
             + "、".join(f"{c.get('name') or c.get('id')}" for c in chars[:16])
             + (" …" if len(chars) > 16 else ""))
    pts = src.get("plot_threads") or []
    L.append(f"线索（{len(pts)}）: "
             + "、".join(f"{t.get('name') or t.get('id')}[{t.get('status') or ''}]"
                         for t in pts[:12])
             + (" …" if len(pts) > 12 else ""))
    rules = src.get("world_rules") or []
    L.append(f"世界规则（{len(rules)}）· 力量体系层数 {src.get('power_system_levels')}")
    if rules:
        L.append("  " + "；".join(
            (f"{r.get('id')}:{r.get('text')}" if r.get('id') and r.get('text') != r.get('id')
             else r.get('text') or r.get('id') or '') for r in rules[:6])
            + (" …" if len(rules) > 6 else ""))
    settings = src.get("settings") or []
    L.append(f"设定条目（{len(settings)}）: " + "、".join(settings[:10])
             + (" …" if len(settings) > 10 else ""))
    mem = src.get("memory") or {}
    events = mem.get("recent_events") or []
    L.append(f"记忆：基线事件 {len(events)} 条 · 碎片 {mem.get('fragment_count')} 条"
             f" · 本章引用 {mem.get('this_chapter_refs')} 条")
    for e in events[:6]:
        participate = "/".join(e.get("participants") or [])
        who = f"（{participate}）" if participate else ""
        L.append(f"  - [{e.get('vol')}-{e.get('ch')}]{e.get('summary')}{who}")
    return "\n".join(L)