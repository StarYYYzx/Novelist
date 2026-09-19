"""分模块审核闸门（ADR-024 v1）。

用户指令映射：核心设定等"系统自由设计的内容"按模块拆分，每模块一个审核开关
（类似代码软件的权限设置）。LLM 生成该模块内容后，若开关为开 → 内容完整展示
给用户（渲染 markdown 评审稿），build 暂停等待处置：

- ``forge approve <module> [--remember]``：可行（--remember = 可行，且该模块
  后续不再审核，开关永久关闭）；
- ``forge revise <module> "修改建议"``：按建议重生成该模块 → 再次展示；
  新旧差异（矛盾）如实列出，已生成正文基于旧设计的风险仅提示、不自动处理
  （复杂修订场景按用户指示暂缓）。

状态存 ``{project}/workspace/forge/review.json``：switches / pending / history。
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

from .state import Blueprint

REVIEW_REL = "workspace/forge/review.json"


# ---- 模块注册表：模块 id → 展示名 / 归属节点 / 蓝图段 ----
# book 节点一次产出全部核心设定段 → 按段拆成 6 个审核模块；
# volume/chapter 节点各对应一个大纲模块（依照系统真实节点谱系分配）。
REVIEW_MODULES: dict[str, dict] = {
    "worldview": {"label": "世界观", "kind": "book",
                  "sections": ["worldview", "time_origin"]},
    "characters": {"label": "角色设计", "kind": "book", "sections": ["characters"]},
    "entities": {"label": "地点与物品", "kind": "book",
                 "sections": ["locations", "items"]},
    "threads": {"label": "伏笔线", "kind": "book", "sections": ["threads"]},
    "style": {"label": "文风", "kind": "book", "sections": ["style"]},
    "volumes": {"label": "卷规划", "kind": "book", "sections": ["volumes"]},
    "outline_volume": {"label": "卷纲", "kind": "volume", "sections": []},
    "outline_chapter": {"label": "章纲", "kind": "chapter", "sections": []},
}


# ---- 配置读写 ----
def _default_config() -> dict:
    """默认全开：安全优先，用户可按模块关（或 approve --remember 永久关）。"""
    return {"switches": {m: True for m in REVIEW_MODULES},
            "pending": {}, "history": []}


def load_review(ws: Any, project_id: str) -> dict:
    path = ws._abs(f"{project_id}/{REVIEW_REL}")  # noqa: SLF001
    if not path.exists():
        return _default_config()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _default_config()
    cfg = _default_config()
    cfg["switches"].update(data.get("switches") or {})
    cfg["pending"] = data.get("pending") or {}
    cfg["history"] = data.get("history") or []
    return cfg


def save_review(ws: Any, project_id: str, cfg: dict) -> None:
    ws.write_json(ws._abs(f"{project_id}/{REVIEW_REL}"), cfg)  # noqa: SLF001


def switch_on(ws: Any, project_id: str, module: str) -> bool:
    if module not in REVIEW_MODULES:
        raise ValueError(f"unknown review module: {module}")
    return bool(load_review(ws, project_id)["switches"].get(module, True))


def set_switch(ws: Any, project_id: str, module: str, on: bool) -> None:
    cfg = load_review(ws, project_id)
    cfg["switches"][module] = bool(on)
    save_review(ws, project_id, cfg)


def pending_modules(ws: Any, project_id: str) -> dict:
    return load_review(ws, project_id)["pending"]


# ---- pending 生命周期 ----
def mark_pending(ws: Any, project_id: str, bp: Blueprint, modules: list[str],
                 *, log_fn: Callable[[str], None] | None = None,
                 vol: int = 0, ch: int = 0) -> dict:
    """对刚生成内容的模块落 pending 并渲染评审稿。返回 module → 评审稿路径。"""
    cfg = load_review(ws, project_id)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    files: dict[str, str] = {}
    for m in modules:
        path = render_review_md(ws, project_id, bp, m, vol=vol, ch=ch)
        prev = cfg["pending"].get(m) or {}
        entry = {"file": path, "node": REVIEW_MODULES[m]["kind"], "at": now}
        if vol:
            entry["vol"] = vol
        if ch:
            entry["ch"] = ch
        if prev.get("suggestions"):  # revise 重生成后保留建议痕迹（仍待审）
            entry["suggestions"] = prev["suggestions"]
        cfg["pending"][m] = entry
        files[m] = path
    save_review(ws, project_id, cfg)
    if log_fn:
        for m, p in files.items():
            log_fn(f"[gate] {REVIEW_MODULES[m]['label']}({m}) 待审核 → {p}")
    return files


def resolve_pending(ws: Any, project_id: str, module: str, *,
                    decision: str, remember: bool = False,
                    note: str = "") -> None:
    """审核通过：清 pending；remember=True 同时永久关该模块开关。"""
    if module not in REVIEW_MODULES:
        raise ValueError(f"unknown review module: {module}")
    cfg = load_review(ws, project_id)
    cfg["pending"].pop(module, None)
    cfg["history"].append({"module": module, "decision": decision,
                           "remember": remember, "note": note[:200],
                           "at": time.strftime("%Y-%m-%d %H:%M:%S")})
    if remember:
        cfg["switches"][module] = False
    save_review(ws, project_id, cfg)


def resolve_all_pending(ws: Any, project_id: str, *, remember: bool = False,
                        note: str = "") -> list[str]:
    """批量批准全部 pending 模块；remember=True 同时永久关这些模块的开关。

    返回批准的模块名列表（按字典序）。放权要留痕：history 里每模块一条
    `decision="approve"`（note 标记 "approve all"），可追溯。
    """
    cfg = load_review(ws, project_id)
    mods = sorted((cfg.get("pending") or {}).keys())
    for m in mods:
        resolve_pending(ws, project_id, m, decision="approve",
                        remember=remember, note=note or "approve all")
    return mods


def set_all_switches(ws: Any, project_id: str, on: bool) -> None:
    """批量开关全部审核模块（`/approve-all all` 的 off 反向操作也走这里）。"""
    cfg = load_review(ws, project_id)
    for m in cfg["switches"]:
        cfg["switches"][m] = bool(on)
    save_review(ws, project_id, cfg)


def approve_all(ws: Any, project_id: str, target: str, *, off: bool = False,
                note: str = "") -> str:
    """`/approve-all <模块|all>`（2026-09-19 拍板：放权机制 + off 恢复审核）。

    - 默认（off=False）：批准目标 pending（`approve --remember` 语义）并**永久关开关**
      ——该模块后续构建不再人工审核。
    - off=True：**重新打开开关**恢复审核（不动 pending——pending 里挂着的仍要处置）。

    返回给用户看的说明文字。放权/收权都写 history（decision 分别为
    `approve-all` / `review-resume`），不静默。
    """
    if target != "all" and target not in REVIEW_MODULES:
        raise ValueError(f"未知模块 {target!r}；可选：all 或 {', '.join(REVIEW_MODULES)}")
    cfg = load_review(ws, project_id)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    if off:
        if target == "all":
            set_all_switches(ws, project_id, True)
            mods = sorted(REVIEW_MODULES)
        else:
            set_switch(ws, project_id, target, True)
            mods = [target]
        cfg = load_review(ws, project_id)
        for m in mods:
            cfg["history"].append({"module": m, "decision": "review-resume",
                                   "remember": False, "note": (note or "approve-all off")[:200],
                                   "at": now})
        save_review(ws, project_id, cfg)
        return f"已恢复审核：{'、'.join(mods)}（后续构建将重新挂起待审）"
    if target == "all":
        mods = resolve_all_pending(ws, project_id, remember=True,
                                   note=note or "approve-all all")
        # pending 为空时也要把开关全关（用户意图是"以后都别审"）
        already_clear = sorted(m for m in REVIEW_MODULES if m not in mods)
        set_all_switches(ws, project_id, False)
        cfg = load_review(ws, project_id)
        for m in already_clear:
            cfg["history"].append({"module": m, "decision": "approve-all",
                                   "remember": True, "note": (note or "approve-all all")[:200],
                                   "at": now})
        save_review(ws, project_id, cfg)
        return ("已放行全部模块（含待审 " + ("、".join(mods) if mods else "无")
                + "）——**后续构建不再人工审核任何模块**（/approve-all all off 恢复）")
    # 单模块：清 pending（如有）+ 关开关
    had_pending = target in (cfg.get("pending") or {})
    resolve_pending(ws, project_id, target, decision="approve-all",
                    remember=True, note=note or "approve-all")
    return (f"已放行模块 {target}" + ("（清了待审）" if had_pending else "（本就不在待审）")
            + "，该模块后续不再审核（/approve-all " + target + " off 恢复）")


def stage_pending_for_revise(ws: Any, project_id: str, module: str,
                             suggestions: str) -> None:
    """把修改建议记入 pending（revise 重生成后仍待审）。"""
    cfg = load_review(ws, project_id)
    if module in cfg["pending"]:
        cfg["pending"][module]["suggestions"] = suggestions[:2000]
    save_review(ws, project_id, cfg)


# ---- 评审稿渲染 ----
def _render_value(v: Any, indent: int = 0) -> list[str]:
    pad = "  " * indent
    out: list[str] = []
    if isinstance(v, dict):
        for k, x in v.items():
            if isinstance(x, (dict, list)):
                out.append(f"{pad}- **{k}**:")
                out.extend(_render_value(x, indent + 1))
            else:
                out.append(f"{pad}- **{k}**: {x}")
    elif isinstance(v, list):
        for x in v:
            if isinstance(x, (dict, list)):
                out.append(f"{pad}-")
                out.extend(_render_value(x, indent + 1))
            else:
                out.append(f"{pad}- {x}")
    else:
        out.append(f"{pad}{v}")
    return out


def render_review_md(ws: Any, project_id: str, bp: Blueprint, module: str,
                     *, vol: int = 0, ch: int = 0) -> str:
    """模块内容完整渲染为 markdown 评审稿（reviews/<module>.md）。返回相对路径。"""
    spec = REVIEW_MODULES[module]
    coord = (f" vol={vol}" if vol else "") + (f" ch={ch}" if ch else "")
    lines = [f"# 审核评审稿：{spec['label']}（{module}）{coord}", "",
             f"- 归属节点：`{spec['kind']}`；处置：`forge approve {module} [--remember]` "
             f"或 `forge revise {module} \"修改建议\"`", ""]
    if module == "outline_chapter":
        # 章纲：读细纲 md 原文（生成侧权威载体）
        gist = ws._abs(f"{project_id}/outline/chapters/{vol}-{ch}.md")  # noqa: SLF001
        lines.append(f"## 第 {vol} 卷第 {ch} 章细纲")
        lines.append("")
        lines.append(gist.read_text(encoding="utf-8") if gist.exists() else "（细纲文件缺失）")
    elif module == "outline_volume":
        row = next((x for x in bp.data.get("volumes") or []
                    if int(x.get("vol") or 0) == vol), None)
        lines.append(f"## 第 {vol} 卷主线")
        lines.append("")
        lines.extend(_render_value(row) if row else ["（卷条目缺失）"])
    else:
        for sec in spec["sections"]:
            val = bp.data.get(sec)
            lines.append(f"## 段 `{sec}`")
            lines.append("")
            if val in (None, {}, []):
                lines.append("（空）")
            else:
                lines.extend(_render_value(val))
            lines.append("")
    rel = f"workspace/forge/reviews/{module}.md"
    ws.write_text(ws._abs(f"{project_id}/{rel}"), "\n".join(lines))  # noqa: SLF001
    return rel


# ---- 差异（revise 时如实告知矛盾/变更） ----
def diff_section(old: Any, new: Any, path: str = "") -> list[str]:
    """确定性递归 diff：新增/删除/修改逐条列出（不做语义裁决）。"""
    out: list[str] = []
    if isinstance(old, dict) and isinstance(new, dict):
        for k in sorted(set(old) | set(new)):
            p = f"{path}.{k}" if path else k
            if k not in old:
                out.append(f"新增 {p}")
            elif k not in new:
                out.append(f"删除 {p}")
            else:
                out.extend(diff_section(old[k], new[k], p))
    elif isinstance(old, list) and isinstance(new, list):
        for i in range(max(len(old), len(new))):
            p = f"{path}[{i}]"
            if i >= len(old):
                out.append(f"新增 {p}")
            elif i >= len(new):
                out.append(f"删除 {p}")
            else:
                out.extend(diff_section(old[i], new[i], p))
    elif old != new:
        out.append(f"修改 {path or '<root>'}: {old!r} → {new!r}")
    return out


def has_generated_prose(ws: Any, project_id: str) -> bool:
    """已生成正文（章节文件）→ 修订设定后需提示旧文风险。"""
    d = ws._abs(f"{project_id}/chapters")  # noqa: SLF001
    try:
        return d.exists() and any(d.iterdir())
    except OSError:
        return False
