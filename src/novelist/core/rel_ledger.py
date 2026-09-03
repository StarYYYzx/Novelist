"""实然关系账本（ADR-023，A2 落地）：从角色视角记忆确定性聚合 pair 状态。

设计（docs/03 ADR-023 D2/D3-b，2026-09-03 定稿）：
- 事实源 = `memory/character_histories/*.json` 中 `kind=perspective` 条目的
  `relations[{who, delta}]`（N4 产出，ADR-020 决策四；实证 35/35 为空后已强化
  PERSPECTIVE_PROMPT 第 4 段为必答 + 方向词表，2026-09-03）。
- 本模块**零新增 LLM 调用**：`rebuild_ledger()` 纯确定性扫描 + 聚合，落
  `memory/relationship_ledger.json`（ADR-016 可再生投影——任何时候可删除重建，
  不是事实源；bible 关系行仍是应然层，本模块绝不改写）。
- 阈值升级（D3-b）：同向 delta 连续 ≥2 事件，或出现 断裂/复合 级变化 →
  `enqueue_flip_proposals()` 生成"关系修订提案"入 enrich pending（人工 `--allow`，
  provenance 同 M3r），不自动覆盖 bible。
- 文件命名偏差说明：ADR-023 原文写 `memory/relationships.json`，但该文件已被
  docs/06 §3.5 占用为"关系变化事件日志"（`MemoryWriter.record_relationship_change`，
  有测试与 RAG 碎片语义）；账本作为派生投影另立 `relationship_ledger.json`，
  两职责不混写一文件。

delta 方向词表：新格式 `对<人名>：<方向>·<变化短语>`（方向∈升温/降温/转向/断裂/复合/不变）；
兼容旧格式自由文本（词表子串启发式分类，见 `_classify_dir`）。
"""

from __future__ import annotations

import json
import re

# 账本文件（相对 project_id）
LEDGER_REL = "memory/relationship_ledger.json"
# 方向词表（与 chronicler.PERSPECTIVE_PROMPT 第 4 段约束一致；解析兼容"不变"）
DIRECTIONS = ("升温", "降温", "转向", "断裂", "复合", "不变")
# 视角条目 event_ref 形如 ev:proj:1:3:e2
_EVENT_RE = re.compile(r":e(\d+)\s*$")
# 新格式前缀：方向 + 可选分隔符 + 变化短语；也接受裸方向词（如"断裂"）
_DIR_PREFIX_RE = re.compile(r"^\s*(升温|降温|转向|断裂|复合|不变)\s*(?:[·:：=,，]\s*(.*))?$")

# 旧格式自由文本启发式词表（子串匹配；断裂/复合 优先于 升温/降温）。
# 注意：不收录"加深"这类纯程度副词（"记恨加深"应由 记恨 判向，避免误判转向）。
_BREAK_WORDS = (
    "决裂", "绝交", "反目", "断交", "翻脸", "成仇", "恩断义绝", "分道扬镳",
    "割袍", "割席", "势不两立", "死敌", "不共戴天", "断裂",
)
_REUNITE_WORDS = ("复合", "和解", "和好", "言和", "冰释", "前嫌", "重修旧好", "破镜重圆")
_UP_WORDS = (
    "信任", "信赖", "亲近", "亲昵", "敬重", "尊敬", "感激", "欣赏", "佩服",
    "青睐", "好感", "爱慕", "心动", "暧昧", "依赖", "依恋", "同盟", "联手",
    "结盟", "知己", "融洽", "缓和", "好转", "升温", "患难与共", "托付",
    "交心", "心疼", "心软", "倚重", "器重", "看重",
)
_DOWN_WORDS = (
    "疏远", "疏离", "猜忌", "怀疑", "起疑", "提防", "防备", "警惕", "忌惮",
    "畏惧", "记恨", "怨恨", "仇视", "仇恨", "敌意", "敌对", "厌恶", "反感",
    "嫌弃", "冷淡", "冷漠", "生分", "芥蒂", "嫌隙", "恶化", "冲突", "戒备",
    "不满", "失望", "不信任",
)


# ---------------------------------------------------------------- delta 解析


def _classify_dir(phrase: str) -> str:
    """旧格式自由文本的方向启发式分类（确定性）。"""
    hit_break = any(w in phrase for w in _BREAK_WORDS)
    hit_reunite = any(w in phrase for w in _REUNITE_WORDS)
    hit_up = any(w in phrase for w in _UP_WORDS)
    hit_down = any(w in phrase for w in _DOWN_WORDS)
    if hit_break:
        return "断裂"
    if hit_reunite:
        return "复合"
    if hit_up and hit_down:
        return "转向"
    if hit_up:
        return "升温"
    if hit_down:
        return "降温"
    return "不变"


def parse_delta(raw: str) -> tuple[str, str]:
    """拆 (方向, 变化短语)。带方向前缀直读；旧自由文本启发式分类。"""
    s = str(raw or "").strip()
    if not s:
        return "不变", ""
    m = _DIR_PREFIX_RE.match(s)
    if m:
        phrase = (m.group(2) or "").strip() or m.group(1)
        return m.group(1), phrase
    return _classify_dir(s), s


# ---------------------------------------------------------------- 读取


def _read_json(p):
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def _read_bible(ws, project_id: str, rel: str):
    return _read_json(ws._abs(f"{project_id}/bible/{rel}"))  # noqa: SLF001


def _load_chars(ws, project_id: str) -> list[dict]:
    chars = _read_bible(ws, project_id, "characters.json")
    return [c for c in chars if isinstance(c, dict) and c.get("id")] if isinstance(chars, list) else []


def _char_by_id(ws, project_id: str) -> dict[str, dict]:
    return {c["id"]: c for c in _load_chars(ws, project_id)}


def _load_ledger(ws, project_id: str) -> dict:
    data = _read_json(ws._abs(f"{project_id}/{LEDGER_REL}"))  # noqa: SLF001
    return data if isinstance(data, dict) else {}


def _iter_delta_records(ws, project_id: str):
    """按全局序 yield 视角关系增量记录（跨角色文件、跨事件）。

    yield dict: view / other / raw / dir / phrase / at / event_ref / ev_idx
    """
    hdir = ws._abs(f"{project_id}/memory/character_histories")  # noqa: SLF001
    if not hdir.is_dir():
        return
    for f in sorted(hdir.glob("*.json")):
        data = _read_json(f)
        if not isinstance(data, dict):
            continue
        cid = str(data.get("char_id") or f.stem)
        for e in data.get("entries") or []:
            if not isinstance(e, dict) or e.get("kind") != "perspective":
                continue
            rels = e.get("relations")
            if not rels:
                continue
            at = e.get("at") if isinstance(e.get("at"), dict) else {}
            evref = str(e.get("event_ref") or "")
            m = _EVENT_RE.search(evref)
            ev_idx = int(m.group(1)) if m else 0
            for r in rels:
                if not isinstance(r, dict):
                    continue
                other = str(r.get("who") or "").strip()
                raw = str(r.get("delta") or "").strip()
                if not other or other == cid or not raw:
                    continue
                d, phrase = parse_delta(raw)
                yield {
                    "view": cid, "other": other, "raw": raw, "dir": d,
                    "phrase": phrase, "at": at, "event_ref": evref,
                    "ev_idx": ev_idx,
                }


# ---------------------------------------------------------------- 确定性聚合


def _bucket_dir(dirs: list[str]) -> str:
    """同一事件（多视角）的方向归并：断裂/复合 优先，升温+降温 同场 → 转向。"""
    if not dirs:
        return "不变"
    if "断裂" in dirs:
        return "断裂"
    if "复合" in dirs:
        return "复合"
    distinct = set(dirs) - {"不变"}
    if len(distinct) >= 2 or "转向" in distinct:
        return "转向"
    if len(distinct) == 1:
        return distinct.pop()
    if "升温" in distinct:
        return "升温"
    if "降温" in distinct:
        return "降温"
    return "不变"


def _flip_trigger(seq: list[str]) -> str | None:
    """D3-b 阈值触发：断裂/复合级单事件即触发；同向升温/降温 连续 ≥2 事件触发。"""
    if not seq:
        return None
    last = seq[-1]
    if last in ("断裂", "复合"):
        return f"{last}级"
    if len(seq) >= 2 and last in ("升温", "降温") and seq[-2] == last:
        return f"同向{last}×2"
    return None


def _aggregate(records) -> dict[str, dict]:
    """records（_iter_delta_records 产物）→ pair 行聚合（不落盘）。"""
    rows: dict[str, dict] = {}
    # pair key -> {buckets: [(ev order, vol, ch, ev_idx, bucket)], by: {view: [rec]}}
    acc: dict[str, dict] = {}
    for rec in records:
        a, b = sorted((rec["view"], rec["other"]))
        pair = f"{a}|{b}"
        st = acc.setdefault(pair, {"a": a, "b": b, "buckets": {}, "by": {}, "order": 0})
        st["order"] += 1
        vol = int(rec["at"].get("vol") or 0)
        ch = int(rec["at"].get("ch") or 0)
        # 桶 = 事件（卷/章/事件序）：同事件的多视角记录归并到一个桶，供方向归并
        key = (vol, ch, rec["ev_idx"])
        bucket = st["buckets"].setdefault(key, {"vol": vol, "ch": ch, "ev_idx": rec["ev_idx"], "recs": []})
        bucket["recs"].append(rec)
        view = rec["view"]
        seq = st["by"].setdefault(view, [])
        seq.append({"seq": len(seq) + 1, "dir": rec["dir"], "phrase": rec["phrase"],
                    "at": rec["at"], "event_ref": rec["event_ref"]})
    for pair, st in acc.items():
        keys = sorted(st["buckets"])
        buckets = [st["buckets"][k] for k in keys]
        seq_dirs = [_bucket_dir([r["dir"] for r in b["recs"]]) for b in buckets]
        latest = buckets[-1]
        last_rec = latest["recs"][-1]
        dirs_latest = [r["dir"] for r in latest["recs"]]
        trend = _bucket_dir(dirs_latest)
        rows[pair] = {
            "pair": pair, "a": st["a"], "b": st["b"],
            "state": last_rec["phrase"][:40],
            "trend": trend,
            "state_from": last_rec["view"],
            "diverged": trend == "转向",
            "by": st["by"],
            "last_event": last_rec["event_ref"],
            "updated_at": last_rec["at"],
            "events": len(buckets),
            "flip": _flip_trigger(seq_dirs),
        }
    return rows


def rebuild_ledger(ws, project_id: str) -> dict:
    """扫描全量视角记忆 → 重建账本并落盘（幂等、可再生）。返回账本 dict。"""
    rows = _aggregate(_iter_delta_records(ws, project_id))
    pairs = [rows[k] for k in sorted(rows)]
    ledger = {
        "schema": "rel-ledger-v1",
        "note": "ADR-023 实然关系账本（可再生投影：删除后由 rebuild_ledger 从 character_histories 重建）",
        "pairs": pairs,
        "meta": {
            "count": len(pairs),
            "flip_count": sum(1 for p in pairs if p.get("flip")),
        },
    }
    ws.write_json(ws._abs(f"{project_id}/{LEDGER_REL}"), ledger)  # noqa: SLF001
    return ledger


def ledger_lines_for(ws, project_id: str, char_id: str, *, limit: int = 3) -> list[str]:
    """该角色相关账本行（可读文本，供 enrich 前情注入——A3 语义补充）。"""
    ledger = _load_ledger(ws, project_id)
    chars = _char_by_id(ws, project_id)
    out: list[str] = []
    for p in ledger.get("pairs") or []:
        if not isinstance(p, dict):
            continue
        if char_id not in (p.get("a"), p.get("b")):
            continue
        a = str((chars.get(p["a"]) or {}).get("name") or p["a"])
        b = str((chars.get(p["b"]) or {}).get("name") or p["b"])
        who = str((chars.get(p.get("state_from") or "") or {}).get("name") or p.get("state_from") or "?")
        at = p.get("updated_at") or {}
        out.append(
            f"- {a} ↔ {b}：{p.get('state') or '?'}（趋势 {p.get('trend') or '不变'}，"
            f"{who} 视角，v{at.get('vol', '?')}c{at.get('ch', '?')}）"
        )
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- 阈值升级（D3-b）


def _subject_and_other(chars_by_id: dict[str, dict], row: dict) -> tuple[str, str]:
    """选提案挂靠卡：已有关系行的一方优先；都没有则取 a。"""
    a, b = row["a"], row["b"]
    for cid, other in ((a, b), (b, a)):
        card = chars_by_id.get(cid) or {}
        for r in card.get("relationships") or []:
            if isinstance(r, dict) and r.get("target") == other:
                return cid, other
    return a, b


def enqueue_flip_proposals(ws, project_id: str, ledger: dict | None = None) -> list[dict]:
    """把阈值触发的 pair 生成"关系修订提案"入 enrich pending（人工 --allow）。

    - 绝不动 bible：提案只进 bible/characters_enrich_pending.json；
    - 去重：bible 关系行已是该短语 / pending 已有同 (卡, target, 短语) → 跳过；
    - 返回本轮新入队的提案列表（空 = 无翻转 / 均已入队）。
    """
    if ledger is None:
        ledger = rebuild_ledger(ws, project_id)
    chars_by_id = _char_by_id(ws, project_id)
    # lazy import：character_enrich 会在 _prior_block 反向引 rel_ledger，避免环
    from .character_enrich import ENRICH_PATH, load_pending  # noqa: PLC0415

    pending = load_pending(ws, project_id)
    seen = {
        (p.get("card_id"), str((p.get("relationships") or [{}])[0].get("target") or ""),
         str((p.get("relationships") or [{}])[0].get("type") or ""))
        for p in pending if isinstance(p, dict)
    }
    added: list[dict] = []
    for row in (ledger.get("pairs") or []):
        if not isinstance(row, dict) or not row.get("flip"):
            continue
        phrase = str(row.get("state") or "").strip()[:30]
        if len(phrase) < 2:
            continue
        subj, other = _subject_and_other(chars_by_id, row)
        card = chars_by_id.get(subj) or {}
        # bible 关系行已是该状态 → 无翻转可写
        cur = next((r for r in (card.get("relationships") or [])
                    if isinstance(r, dict) and r.get("target") == other), None)
        if isinstance(cur, dict) and str(cur.get("type") or "") == phrase:
            continue
        subj_name = str(card.get("name") or subj)
        other_name = str((chars_by_id.get(other) or {}).get("name") or other)
        if (subj, other_name, phrase) in seen:
            continue
        seen.add((subj, other_name, phrase))
        item = {
            "card_id": subj,
            "card_name": subj_name,
            "missing": ["relationships"],
            "age": None,
            "relationships": [{"target": other_name, "type": phrase}],
            "behavior_rules": [],
            "reason": (
                f"ADR-023 阈值升级：{subj_name}↔{other_name} 关系{row.get('trend') or '?'}"
                f"（{row['flip']}），last={row.get('last_event') or '?'}；待人工 --allow "
                "确认是否改写 bible 应然关系行（provenance 同 M3r）"
            ),
        }
        pending.append(item)
        added.append(item)
    if added:
        p = ws._abs(f"{project_id}/bible/{ENRICH_PATH}")  # noqa: SLF001
        p.parent.mkdir(parents=True, exist_ok=True)
        ws.write_json(p, pending)
    return added
