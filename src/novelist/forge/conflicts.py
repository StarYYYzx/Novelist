"""构建期冲突的**人工裁决**队列（2026-09-16 拍板）。

## 为什么不是自动处理

真机事故里，book 节点把「因果」输出成了**第二条主线**（两次生成还各用了不同女主名），
撞上「主线唯一」硬约束后整节点作废——世界观/人物/伏笔/文风一起丢。
用户拍板：**这类结构性冲突由用户决定**，系统既不静默改结构，也不拿"整节点失败"当唯一出路。

于是流程改成三段：

1. **节点侧**（`nodes._apply_lines_skeleton` / `_apply_threads`）：
   检测到冲突时**保留第一条**（先出现/与骨架同 id 的那条），冲突候选**不写进 bible**，
   而是连同完整内容落到本模块的队列里，并在 warn 里给出裁决命令。
2. **人工侧**（`novelist forge conflicts` / console `/conflicts` `/resolve`）：
   查看待裁决项、选择处置方式。
3. **裁决侧**（`resolve_conflict`）：
   按选择把候选写回蓝图（或丢弃），随即 `bp.save` + `sync_bible`。

## 与 ADR-024 审核闸门的关系

审核闸门（`review.json`）管"模块产物要不要通过"；本模块管"两条互相冲突的**结构声明**
留哪条"。前者是发布前的人工签收，后者是冲突时的人工裁决——都不阻塞构建继续跑，
但都会在构建摘要里计数，避免静默。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from ..storage.workspace import Workspace
from .state import Blueprint

CONFLICTS_REL = "workspace/forge/conflicts.json"

# 各类冲突的合法处置（顺序即"选项展示顺序"）
LINE_OPTIONS = ("keep-first", "replace", "subplot", "merge")
THREAD_OPTIONS = ("merge", "keep-both", "drop-new")

_OPTIONS_BY_KIND = {"lines": LINE_OPTIONS, "threads": THREAD_OPTIONS}

# 归一 id：去掉非字母数字汉字并小写——`pt:lingxiang_jinhua` 与 `pt:lingxiangjinhua` 视为同一个
_NORM_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]")


def norm_id(value: Any) -> str:
    """冲突检测用的 id 归一（确定性、零 LLM）。"""
    return _NORM_RE.sub("", str(value or "").lower())


def _path(ws: Workspace, project_id: str):
    return ws._abs(f"{project_id}/{CONFLICTS_REL}")  # noqa: SLF001


def load_conflicts(ws: Workspace, project_id: str) -> dict:
    """读取冲突账本（缺失/损坏时给空档，绝不因它中断构建）。"""
    p = _path(ws, project_id)
    if not p.exists():
        return {"open": [], "resolved": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"open": [], "resolved": []}
    if not isinstance(data, dict):
        return {"open": [], "resolved": []}
    data.setdefault("open", [])
    data.setdefault("resolved", [])
    return data


def _save(ws: Workspace, project_id: str, data: dict) -> None:
    ws.write_json(_path(ws, project_id), data)


def open_conflicts(ws: Workspace, project_id: str, *, kind: str | None = None) -> list[dict]:
    """待裁决列表（可按 kind 过滤）。"""
    items = load_conflicts(ws, project_id)["open"]
    return [c for c in items if kind is None or c.get("kind") == kind]


def add_conflict(
    ws: Workspace,
    project_id: str,
    *,
    kind: str,
    node: str,
    summary: str,
    payload: dict,
    suggested: str,
) -> dict:
    """登记一条待裁决冲突（幂等：同一 candidate id 已挂起则不重复登记）。"""
    data = load_conflicts(ws, project_id)
    cand_id = str((payload.get("candidate") or {}).get("id") or "")
    for c in data["open"]:
        if c.get("kind") == kind and str((c.get("payload") or {}).get("candidate", {}).get("id")) == cand_id:
            return c
    cid = f"cf:{kind}:{len(data['open']) + len(data['resolved']) + 1}"
    item = {
        "id": cid,
        "kind": kind,
        "node": node,
        "summary": summary,
        "options": list(_OPTIONS_BY_KIND.get(kind, ())),
        "suggested": suggested,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        "payload": payload,
    }
    data["open"].append(item)
    _save(ws, project_id, data)
    return item


def _ensure_anchor(bp: Blueprint, section: str, row: dict) -> None:
    """保证被裁决锚定的行确实在蓝图里。

    冲突 payload 里存着当事双方的完整内容；裁决时若锚定行已不在蓝图（人为删过、或
    当初只在内存里未落盘），按 payload 补回来——否则 `keep-both` / `merge` 会静默丢掉一半。
    """
    rid = str(row.get("id") or "")
    if rid and bp.find_by_id(section, rid) is None:
        bp.upsert(section, dict(row))


def _apply_lines(ws: Workspace, project_id: str, bp: Blueprint, item: dict,
                 choice: str) -> tuple[str, tuple[str, ...]]:
    payload = item.get("payload") or {}
    existing = dict(payload.get("existing") or {})
    cand = dict(payload.get("candidate") or {})
    _ensure_anchor(bp, "lines", existing)
    eid = str(existing.get("id") or "")
    cid = str(cand.get("id") or "")
    if choice == "keep-first":
        return f"保持主线 {eid}，丢弃候选 {cid}", ()
    if choice == "replace":
        bp.data["lines"] = [x for x in (bp.section("lines") or []) if x.get("id") != eid]
        bp.upsert("lines", cand)
        bp.set_provenance(f"lines[{cid}]", "user", 1.0)
        # 旧主线同时要从 bible 里清掉：sync_bible 是"按 id 合并"语义，
        # 蓝图删行不会删盘上行（见 _merge_bible_rows 的 keep_extra）
        return f"主线替换为 {cid}（原 {eid} 已移除）", (eid,)
    if choice == "subplot":
        downgraded = dict(cand)
        downgraded["kind"] = "subplot"
        bp.upsert("lines", downgraded)
        bp.set_provenance(f"lines[{cid}].kind", "user", 1.0)
        return f"{cid} 已作为支线并入（kind=subplot）", ()
    if choice == "merge":
        merged = dict(existing)
        merged["desc"] = f"{existing.get('desc', '')}；{cand.get('desc', '')}".strip("；")
        members = list(dict.fromkeys([*(existing.get("members") or []), *(cand.get("members") or [])]))
        merged["members"] = members
        old_target = existing.get("target") or {}
        new_target = cand.get("target") or {}
        if int(new_target.get("vol") or 0) > int(old_target.get("vol") or 0):
            merged["target"] = new_target
        bp.upsert("lines", merged)
        bp.set_provenance(f"lines[{eid}].desc", "user", 1.0)
        return f"候选 {cid} 已并入主线 {eid}（desc 拼接、members 并集、target 取更远者）", ()
    raise ValueError(f"unknown lines choice: {choice!r}（可选 {list(LINE_OPTIONS)}）")


def _apply_threads(ws: Workspace, project_id: str, bp: Blueprint, item: dict,
                   choice: str) -> tuple[str, tuple[str, ...]]:
    payload = item.get("payload") or {}
    existing = dict(payload.get("existing") or {})
    cand = dict(payload.get("candidate") or {})
    _ensure_anchor(bp, "threads", existing)
    eid = str(existing.get("id") or "")
    cid = str(cand.get("id") or "")
    if choice == "drop-new":
        return f"丢弃候选 {cid}，保留 {eid}", ()
    if choice == "keep-both":
        kept = dict(cand)
        kept["id"] = f"{cid}_b" if not cid.endswith("_b") else f"{cid}_c"
        bp.upsert("threads", kept)
        bp.set_provenance(f"threads[{kept['id']}]", "user", 1.0)
        return f"两条都保留：{eid} 与 {kept['id']}", ()
    if choice == "merge":
        merged = dict(existing)
        if str(cand.get("desc") or "") and cand.get("desc") != existing.get("desc"):
            merged["desc"] = f"{existing.get('desc', '')}（另一说法：{cand.get('desc')}）"
        for key in ("carrier", "target_vol", "scope"):
            if not merged.get(key) and cand.get(key):
                merged[key] = cand[key]
        bp.upsert("threads", merged)
        bp.set_provenance(f"threads[{eid}].desc", "user", 1.0)
        return f"候选 {cid} 已并入伏笔 {eid}", ()
    raise ValueError(f"unknown threads choice: {choice!r}（可选 {list(THREAD_OPTIONS)}）")


def resolve_conflict(ws: Workspace, project_id: str, conflict_id: str, choice: str) -> str:
    """按人工选择处置一条冲突；返回结果说明。会写蓝图 + bible。"""
    from .nodes import sync_bible

    data = load_conflicts(ws, project_id)
    item = next((c for c in data["open"] if c.get("id") == conflict_id), None)
    if item is None:
        raise ValueError(f"no open conflict: {conflict_id}")
    kind = str(item.get("kind") or "")
    if choice not in _OPTIONS_BY_KIND.get(kind, ()):
        raise ValueError(f"{conflict_id}（{kind}）不支持 {choice!r}；可选 {list(_OPTIONS_BY_KIND.get(kind, ()))}")
    bp = Blueprint.load(ws, project_id)
    if kind == "lines":
        note, purge = _apply_lines(ws, project_id, bp, item, choice)
    elif kind == "threads":
        note, purge = _apply_threads(ws, project_id, bp, item, choice)
    else:
        raise ValueError(f"unknown conflict kind: {kind!r}")
    bp.save(ws, project_id)
    sync_bible(ws, project_id, bp)
    _purged = _purge_bible_lines(ws, project_id, purge)
    if _purged:
        note += f"（bible/lines.json 同步清掉 {_purged} 行）"
    data["open"] = [c for c in data["open"] if c.get("id") != conflict_id]
    item["choice"] = choice
    item["note"] = note
    item["resolved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
    data["resolved"].append(item)
    _save(ws, project_id, data)
    return note


def _purge_bible_lines(ws: Workspace, project_id: str, ids: tuple[str, ...]) -> int:
    """从 `bible/lines.json` 删除指定线索行，返回删除条数。

    为什么需要显式删：`sync_bible` 走 `_merge_bible_rows`（**按 id 合并**，`keep_extra`
    语义保护工厂/enrich 追加行）——"蓝图里删掉一行"不会让盘上行消失。线索账本的应然骨架
    就是蓝图，所以裁决删除时必须在 bible 侧同步清掉，否则 bible 会留下两条主线
    （正是 2026-09-16 事故的形态）。
    """
    if not ids:
        return 0
    p = ws.bible_path(project_id, "lines")
    try:
        rows = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(rows, list):
        return 0
    drop = set(ids)
    kept = [r for r in rows if not (isinstance(r, dict) and r.get("id") in drop)]
    removed = len(rows) - len(kept)
    if removed:
        ws.write_json(p, kept)
    return removed
