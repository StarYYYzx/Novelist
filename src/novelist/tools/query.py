"""结构化查询工具（ADR-036 M3ac-2，2026-09-19）：list_chapters / get_bible /
get_outline / list_conflicts / get_worldstate——全部 safe 只读。

为什么需要：对话 agent 与证据环此前只能靠 `read_file`/`grep_text` 猜 JSON 布局
（"文件布局幻觉"，脆且费 token）。本模块把最高频的**结构化**读取做成一等工具，
让 agent 不必知道文件长什么样。

纪律：safe 级 = 门禁自动放行 → 新增必须在 `scripts/check.py` 的
`_SAFE_TOOL_ALLOWLIST` 登记（G3 机械检查）；同时登记进 `core/tools.EVIDENCE_TOOL_NAMES`
（证据环同用，T-1）。
"""

from __future__ import annotations

import json

from ..core.tools import LEVEL_SAFE, SCHEMA_FAIL, Tool, fail, ok
from ..storage.workspace import Workspace

# bible 可读的段（白名单——防止拿它当任意文件读，read_file 才是那个通道）
_BIBLE_SECTIONS = ("characters", "worldview", "style", "locations", "plot_threads",
                   "items", "skills", "settings", "lines", "timeline", "worldstate")


def tools(ws: Workspace) -> list[Tool]:

    def _read_json(pid: str, rel: str):
        p = ws._abs(f"{pid}/{rel}")  # noqa: SLF001
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _list_chapters(session, params, budget=None):
        """章节全景：已定稿 + 草稿 + 细纲，各章状态一行。"""
        pid = session.project_id
        rows: list[dict] = []
        out_dir = ws._abs(f"{pid}/outline/chapters")  # noqa: SLF001
        gists = {}
        if out_dir.exists():
            for f in sorted(out_dir.glob("*.md")):
                vol, _, ch = f.stem.partition("-")
                gists[(vol, ch)] = "outline"

        def _scan(rel: str, status: str) -> None:
            d = ws._abs(f"{pid}/{rel}")  # noqa: SLF001
            if not d.exists():
                return
            for f in sorted(d.glob("*.md")):
                vol, _, ch = f.stem.partition("-")
                rows.append({"vol": vol, "ch": ch, "status": status,
                             "chars": len(f.read_text(encoding="utf-8"))})

        _scan("chapters", "published")
        _scan("drafts/chapters", "draft")
        for row in rows:
            if (row["vol"], row["ch"]) in gists:
                row["has_outline"] = True
        only_gist = [{"vol": v, "ch": c, "status": "outline", "chars": 0,
                      "has_outline": True}
                     for (v, c) in gists
                     if not any(r["vol"] == v and r["ch"] == c for r in rows)]
        return ok(data={"chapters": rows + only_gist,
                        "total": len(rows) + len(only_gist)})

    def _get_bible(session, params, budget=None):
        """读 bible 某分区；给 id 精确取一条。`blueprint` / `blueprint:<子段>` 读蓝图
        （2026-09-19 真机：agent 不知道布局，反复 read_file 52KB 蓝图打转）。"""
        section = str(params.get("section") or "")
        if section == "blueprint" or section.startswith("blueprint:"):
            bp_data = _read_json(session.project_id, "workspace/forge/blueprint.json")
            if bp_data is None:
                return ok(data={"found": False, "hint": "尚无蓝图（先 forge seed/build）"})
            if section == "blueprint":
                counts = {k: (len(v) if isinstance(v, list) else "dict")
                          for k, v in bp_data.items() if k not in ("provenance",)}
                return ok(data={"found": True, "keys": counts,
                                "meta": {k: (bp_data.get("meta") or {}).get(k)
                                         for k in ("title", "genre", "logline")},
                                "hint": "取子段用 blueprint:characters / blueprint:worldview 等"})
            sub = section.split(":", 1)[1]
            val = bp_data.get(sub)
            if val is None:
                return ok(data={"found": False, "sub": sub,
                                "available": sorted(bp_data.keys())})
            body = json.dumps(val, ensure_ascii=False)
            return ok(data={"found": True, "sub": sub,
                            "content": body[:8000], "chars": len(body),
                            "truncated": len(body) > 8000})
        if section not in _BIBLE_SECTIONS:
            return fail(SCHEMA_FAIL,
                        {"error": f"unknown section {section!r}",
                         "available": list(_BIBLE_SECTIONS) + ["blueprint", "blueprint:<子段>"]})
        data = _read_json(session.project_id, f"bible/{section}.json")
        if data is None:
            return ok(data={"section": section, "found": False})
        want_id = str(params.get("id") or "").strip()
        if want_id and isinstance(data, list):
            hit = next((r for r in data
                        if isinstance(r, dict) and str(r.get("id")) == want_id), None)
            return ok(data={"section": section, "id": want_id,
                            "found": hit is not None, "item": hit})
        # 全段回传时截断防爆：list 段先给 id+摘要清单
        if isinstance(data, list):
            brief = []
            for r in data[:200]:
                if isinstance(r, dict):
                    brief.append({k: r.get(k) for k in ("id", "name", "desc", "role")
                                  if r.get(k)})
                else:
                    brief.append(r)
            return ok(data={"section": section, "found": True, "count": len(data),
                            "items_brief": brief,
                            "hint": "带 id 参数可取单条全文"})
        return ok(data={"section": section, "found": True, "item": data})

    def _get_outline(session, params, budget=None):
        """读细纲：vol/ch 必填；无参时给 volumes.json 总览。"""
        pid = session.project_id
        vol, ch = params.get("vol"), params.get("ch")
        if vol is None:
            data = _read_json(pid, "outline/volumes.json")
            return ok(data={"found": data is not None, "volumes": data})
        try:
            vol_i, ch_i = int(vol), int(ch) if ch is not None else None
        except (TypeError, ValueError):
            return fail(SCHEMA_FAIL, {"error": "vol/ch 必须是整数"})
        if ch_i is None:
            return fail(SCHEMA_FAIL, {"error": "读卷总纲用 vol；读章细纲要 vol+ch 一起给"})
        p = ws._abs(f"{pid}/outline/chapters/{vol_i}-{ch_i}.md")  # noqa: SLF001
        if not p.exists():
            return ok(data={"found": False, "vol": vol_i, "ch": ch_i})
        text = p.read_text(encoding="utf-8")
        return ok(data={"found": True, "vol": vol_i, "ch": ch_i,
                        "outline": text[:4000], "chars": len(text),
                        "truncated": len(text) > 4000})

    def _list_conflicts(session, params, budget=None):
        """待裁决结构冲突清单（裁决入口 /conflicts 或 forge conflicts）。"""
        from ..forge.conflicts import open_conflicts

        items = open_conflicts(ws, session.project_id)
        return ok(data={"open": [{"id": c.get("id"), "kind": c.get("kind"),
                                  "summary": c.get("summary"),
                                  "options": c.get("options"),
                                  "suggested": c.get("suggested")}
                                 for c in items],
                        "count": len(items),
                        "hint": "裁决由用户执行 /resolve <id> <选项>，agent 只可分析与建议"})

    def _get_worldstate(session, params, budget=None):
        """实然状态（ADR-019）：时间锚 + 人物硬状态。给 character_id 取单人。"""
        pid = session.project_id
        data = _read_json(pid, "bible/worldstate.json")
        if data is None:
            return ok(data={"found": False,
                            "hint": "worldstate 未生成（构建后首次生成时才合成）"})
        cid = str(params.get("character_id") or "").strip()
        chars = (data.get("characters") or {})
        if cid:
            hit = chars.get(cid)
            if hit is None:
                # 宽松匹配：允许传名字
                hit = next((v for k, v in chars.items()
                            if isinstance(v, dict)
                            and (v.get("name") == cid or k.endswith(cid))), None)
            return ok(data={"found": hit is not None, "character_id": cid,
                            "state": hit,
                            "time": (data.get("time") or {})})
        brief = {k: {f: v.get(f) for f in ("level", "location", "condition", "status")
                     if isinstance(v, dict) and v.get(f) is not None}
                 for k, v in chars.items() if isinstance(v, dict)}
        return ok(data={"found": True, "time": (data.get("time") or {}),
                        "characters": brief, "count": len(brief),
                        "hint": "带 character_id 取单人全量状态"})

    return [
        Tool("list_chapters", "章节全景：已定稿/草稿/细纲各章状态与字数", LEVEL_SAFE,
             _list_chapters, {}),
        Tool("get_bible", "读设定圣经某分区（characters/worldview/style/plot_threads/lines 等）；"
                          "无 id 给清单，带 id 取单条全文", LEVEL_SAFE, _get_bible,
             {"section": {"type": "string"}, "id": {"type": "string"}},
             required=["section"]),
        Tool("get_outline", "读细纲：vol+ch 取章细纲全文；只给 vol 取卷规划；不给取全书卷总览",
             LEVEL_SAFE, _get_outline,
             {"vol": {"type": "integer"}, "ch": {"type": "integer"}}),
        Tool("list_conflicts", "列出待裁决的结构冲突（第二条主线/重名伏笔），含可选裁决项",
             LEVEL_SAFE, _list_conflicts, {}),
        Tool("get_worldstate", "实然状态：当前时间锚 + 人物硬状态（境界/位置/状态）",
             LEVEL_SAFE, _get_worldstate,
             {"character_id": {"type": "string"}}),
    ]
