"""人物数据补喂（P0-A，M3r）：为缺料人物卡提案 relationships / behavior_rules / age。

背景（prompt 作用审计 P0-A）：`produce_chapter` 的注入面已齐（B-02 → build_chapter_context
+ ADR-020 cast_injection 事件级人物卡注入），但 bible 数据质量是断点——yelan3 实测 26 张卡
仅 3 张有 relationships、0 张有 behavior_rules，`render_cards` 注入的是无料卡：模型只能现编
关系 → 行为漂移 / 关系前后矛盾 / AI 味。

设计（对齐 P0-B 的追认闸门哲学）：
- 新数据走**提案 → 待人工确认（pending）→ 入档**通道，绝不自动改写 bible。
- LLM 提案基于**实然**（ADR-011）：该卡 character_histories + 涉卡 plot_events + 全员名册，
  保证"应然设定"不与已发生事实冲突。
- 确定性闸门**零模型调用**：不串卡（name 必须等于目标卡）/ 不造人（target ∈ 名册，含别名，
  解析为 id）/ 无自指 / 行为规则 2-3 条可执行 / age 合法。
- 合并策略（2026-09-02 用户拍板）：同 target 重复关系**采纳提案详细描述**（主角级卡的旧
  2-4 字简略句如"寄生"被提案完整句覆盖），新 target 追加（单卡 ≤ MAX_RELS 条）。
- 确认走 `novelist enrich-pending --allow/--deny`（settings-pending 同款）；pending 条目按
  card_id 落键，allow 时合并进 characters.json 并打 provenance（origin=enrich）。

单元测试绝不真调 LLM（docs/09 §2.1）——provider 用 StubLLM。
"""

from __future__ import annotations

import json

from .character_factory import _char_index  # noqa: WPS437 - 同包只读 helper 复用（M3r 后置重构候选）
from .llm import LLMMessage, LLMRequest

ENRICH_PATH = "characters_enrich_pending.json"   # 相对 bible/
MAX_RELS = 3                                     # 单卡单次提案关系上限
MAX_RULES = 3                                    # 行为规格上限（与工厂闸门一致）
CONFIDENCE = 0.6                                 # 补喂产卡置信度（LLM 提案，人工确认后入档）


def _read_bible_json(ws, project_id: str, rel: str):
    p = ws._abs(f"{project_id}/bible/{rel}")
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def _write_bible_json(ws, project_id: str, rel: str, data) -> None:
    p = ws._abs(f"{project_id}/bible/{rel}")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_memory_json(ws, project_id: str, rel: str):
    p = ws._abs(f"{project_id}/memory/{rel}")
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


# ---------------------------------------------------------------- 缺料判定


def needs_enrichment(card: dict) -> list[str]:
    """缺料清单：缺 relationships 或缺 behavior_rules（P0-A 补喂目标字段）。"""
    out: list[str] = []
    if not card.get("relationships"):
        out.append("relationships")
    if not card.get("behavior_rules"):
        out.append("behavior_rules")
    return out


# ---------------------------------------------------------------- 提案前情


def _card_line(c: dict) -> str:
    """全员名册单卡一行（给 LLM 的关系候选池；同时是闸门的 id 解析依据）。"""
    name = str(c.get("name") or "?")
    g = str(c.get("gender") or "unknown")
    p = c.get("power") or {}
    traits = "、".join(str(t) for t in (c.get("core_traits") or [])[:3])
    arc = str(c.get("arc") or "")[:60]
    rels = [f"{r.get('target')}({r.get('type')})" for r in (c.get("relationships") or [])
            if isinstance(r, dict)]
    rels_txt = "；".join(rels[:4]) if rels else "无"
    return (f"· {name}｜{g}｜{p.get('level') or '?'}｜{p.get('faction') or '?'}"
            f"｜性格:{traits or '?'}｜关系:{rels_txt}｜弧线:{arc}")


def _roster_block(ws, project_id: str) -> str:
    """全员名册文本块（关系钩子只能指向这里面的名字/别名）。"""
    chars = _read_bible_json(ws, project_id, "characters.json") or []
    lines = [_card_line(c) for c in chars if isinstance(c, dict) and c.get("id")]
    if not lines:
        return "（无）"
    return "\n".join(lines)


def _prior_block(ws, project_id: str, char_id: str) -> str:
    """实然前情块：该卡历史档案 + 涉卡事件 + 主线（应然不得与已发生事实冲突）。"""
    out: list[str] = []
    hist = _read_memory_json(ws, project_id, f"character_histories/{char_id}.json")
    if hist and hist.get("entries"):
        evs = [str(e.get("summary") or "") for e in hist["entries"]][-12:]
        out.append("【该角色已发生的经历】（补的关系/行为不得与之冲突）")
        out.extend(f"- {s}" for s in evs if s)
    events = _read_memory_json(ws, project_id, "plot_events.json")
    if isinstance(events, list):
        mine = [e for e in events
                if isinstance(e, dict) and char_id in (e.get("participants") or [])][-6:]
        if mine:
            out.append("【涉该角色的事件】")
            out.extend(f"- {str(e.get('summary') or '')[:120]}" for e in mine)
    # A3 补充：实然关系账本状态行（ADR-023 账本，勿与已观测的关系趋势冲突）
    from .rel_ledger import ledger_lines_for  # noqa: PLC0415 - 反向引用防环

    try:
        rel_lines = ledger_lines_for(ws, project_id, char_id, limit=3)
    except Exception:  # noqa: BLE001 - 账本缺失/异常不影响提案
        rel_lines = []
    if rel_lines:
        out.append("【实然关系账本（该角色最近关系状态/趋势）】")
        out.extend(rel_lines)
    return "\n".join(out) if out else "（无前情记录，该角色未正式登场）"


# ---------------------------------------------------------------- 确定性闸门


def check_proposal(card: dict, proposal: dict, *, ws, project_id: str) -> list[str]:
    """提案闸门（零模型调用）。返回拒绝原因列表（空 = 通过）。"""
    reasons: list[str] = []
    name = str(proposal.get("name") or "").strip()
    card_name = str(card.get("name") or "")
    if name != card_name:
        reasons.append(f"串卡：提案 name={name!r} ≠ 目标卡 {card_name!r}")
        return reasons  # 串卡直接拒，后续闸门无意义
    idx = _char_index(ws, project_id)
    my_id = str(card.get("id") or "")
    existing_targets = {
        str(r.get("target") or "") for r in (card.get("relationships") or [])
        if isinstance(r, dict)}
    seen: set[str] = set()
    for r in (proposal.get("relationships") or [])[:MAX_RELS]:
        if not isinstance(r, dict):
            reasons.append("关系条目不是对象")
            continue
        tgt = str(r.get("target") or "").strip()
        rel = str(r.get("type") or "").strip()
        if not tgt or tgt not in idx:
            reasons.append(f"关系指向不存在角色：{tgt!r}")
            continue
        tid = idx[tgt]
        if tid == my_id:
            reasons.append(f"自指关系被拒：{card_name} 不能指向自己")
            continue
        if tid in seen or tid in existing_targets:
            continue  # 重复 target（同批/已有）：跳过该条——不拒整卡，rules/age 仍有效
        if not rel or len(rel) > 30:
            reasons.append(f"关系描述须 1-30 字（现 {len(rel)} 字）：{rel!r}")
            continue
        seen.add(tid)
    rules = [str(x) for x in (proposal.get("behavior_rules") or []) if str(x).strip()]
    if not 2 <= len(rules) <= MAX_RULES:
        reasons.append(f"行为规格须 2-{MAX_RULES} 条可执行规则（现 {len(rules)} 条）")
    age = proposal.get("age")
    if age is not None:
        try:
            n = int(age)
            if not 10 <= n <= 3000:
                reasons.append(f"age 越界（{n}，须 10-3000 或 null）")
        except (TypeError, ValueError):
            reasons.append(f"age 非数字：{age!r}")
    return reasons


# ---------------------------------------------------------------- 语义闸门（A3 剩余，2026-09-04）
#
# 词级信号、零模型调用。词表是天花板——能挡明显矛盾，隐晦冲突挡不住，
# 兜底是人工确认（pending 通道本来就不自动入档）。

# 亲近/坦诚类表述（提案关系描述 & 账本状态通用）
_CLOSE_WORDS = ("坦诚", "信任", "托付", "亲传", "挚友", "恩师", "亲密", "推心置腹",
                "毫无保留", "救命", "情深")
# 隐瞒/试探/戒备类（账本状态侧的"表面平常、暗中另有动作"）
_HIDDEN_WORDS = ("底细", "试探", "暗中", "猜忌", "提防", "戒备", "调查", "盯上",
                 "列为目标", "留意", "观察")
# 敌对类
_HOSTILE_WORDS = ("仇", "恨", "背叛", "敌对", "不共戴天", "追杀", "灭门")
# 未写章细纲的"首次"类前提词 vs 提案的"旧识"类断言
_FIRST_TIME_WORDS = ("初次", "首次", "第一次", "初见", "初识", "素不相识", "素未谋面")
_OLD_TIE_WORDS = ("旧识", "旧交", "旧部", "多年", "老友", "故友", "熟识", "早已相识",
                  "旧好", "多年未见")


def _ledger_state_for(ws, project_id: str, a_id: str, b_id: str) -> str:
    """实然账本中 (a,b) 双向对的当前状态文本（无记录返回空）。"""
    ledger = _read_memory_json(ws, project_id, "relationship_ledger.json")
    if not isinstance(ledger, dict):
        return ""
    for p in ledger.get("pairs") or []:
        if not isinstance(p, dict):
            continue
        if {str(p.get("a") or ""), str(p.get("b") or "")} == {a_id, b_id}:
            return str(p.get("state") or "")
    return ""


def _unwritten_key_events(ws, project_id: str) -> list[tuple[int, int, str]]:
    """未写章的 key_events 文本行 [(vol, ch, 事件文本)]。

    判定：outline/chapters/{v}-{c}.md 存在而 drafts/chapters/{v}-{c}.md 不存在。
    """
    import re as _re

    outline_dir = ws._abs(f"{project_id}/outline/chapters")
    if not outline_dir.is_dir():
        return []
    out: list[tuple[int, int, str]] = []
    for f in sorted(outline_dir.glob("*.md")):
        m = _re.match(r"(\d+)-(\d+)", f.stem)
        if not m or ws._abs(f"{project_id}/drafts/chapters/{f.stem}.md").exists():
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        for ln in text.splitlines():
            if ln.strip().startswith("key_events:"):
                try:
                    evs = json.loads(ln.split(":", 1)[1])
                except ValueError:
                    evs = []
                out.extend((int(m.group(1)), int(m.group(2)), str(e))
                           for e in evs if e)
    return out


def semantic_gate(card: dict, proposal: dict, *, ws, project_id: str
                  ) -> tuple[list[str], list[str]]:
    """A3 语义闸门（词级，零模型调用）。返回 (拒绝原因, 提案备注)。

    - 账本冲突 → **拒绝**（实然账本已观测的关系态与提案断言直接矛盾）；
    - 未写章细纲前提冲突 → **备注**（写进 pending 的 notes，人工拍板——
      细纲是计划态，可能本身就待改，不替人做主）。
    """
    from .character_factory import _char_index  # noqa: PLC0415 - 同源复用

    reasons: list[str] = []
    notes: list[str] = []
    idx = _char_index(ws, project_id)
    my_id = str(card.get("id") or "")
    for r in (proposal.get("relationships") or [])[:MAX_RELS]:
        if not isinstance(r, dict):
            continue
        tid = idx.get(str(r.get("target") or ""))
        rel = str(r.get("type") or "")
        if not tid or tid == my_id:
            continue
        # ① vs 实然关系账本
        state = _ledger_state_for(ws, project_id, my_id, tid)
        if state:
            state_hidden = any(w in state for w in _HIDDEN_WORDS)
            state_close = any(w in state for w in _CLOSE_WORDS)
            state_hostile = any(w in state for w in _HOSTILE_WORDS)
            rel_close = any(w in rel for w in _CLOSE_WORDS)
            rel_hostile = any(w in rel for w in _HOSTILE_WORDS)
            rel_old = any(w in rel for w in _OLD_TIE_WORDS)
            if (state_hidden or state_hostile) and rel_close:
                reasons.append(
                    f"与实然账本冲突：与「{r.get('target')}」账本状态为「{state}」"
                    f"（隐瞒/敌对），提案却断言亲近（{rel!r}）")
            elif state_close and rel_hostile:
                reasons.append(
                    f"与实然账本冲突：与「{r.get('target')}」账本状态为「{state}」"
                    f"（亲近），提案却断言敌对（{rel!r}）")
            elif rel_old and ("素不相识" in state or "素未谋面" in state):
                reasons.append(
                    f"与实然账本冲突：与「{r.get('target')}」账本仍是「{state}」，"
                    f"提案却断言旧识（{rel!r}）")
        # ② vs 未写章细纲前提（计划态 → 只备注）
        tgt_name = str(r.get("target") or "")
        for vol, ch, ev in _unwritten_key_events(ws, project_id):
            if tgt_name not in ev:
                continue
            if (any(w in ev for w in _FIRST_TIME_WORDS)
                    and any(w in rel for w in _OLD_TIE_WORDS)):
                notes.append(
                    f"细纲前提留意：未写章 {vol}-{ch} 事件「{ev[:60]}」含「首次」意图，"
                    f"提案关系 {rel!r} 断言旧识——请人工确认是否冲突")
                break
    return reasons, notes


def _apply_one_card(card: dict, proposal: dict, *, ws, project_id: str) -> dict:
    """闸门通过后的合并（新值并入，已有值保全）。返回更新后的卡。"""
    idx = _char_index(ws, project_id)
    # relationships：按 target id 判重——同 target 采纳提案描述（拍板 2026-09-02，
    # 旧 2-4 字简略句被提案完整句覆盖）；新 target 追加（[:MAX_RELS] 与闸门同口径）
    rels = list(card.get("relationships") or [])
    at = {str(r.get("target") or ""): i for i, r in enumerate(rels) if isinstance(r, dict)}
    for r in (proposal.get("relationships") or [])[:MAX_RELS]:
        if not isinstance(r, dict):
            continue
        tid = idx.get(str(r.get("target") or ""))
        if not tid:
            continue
        new_type = str(r.get("type") or "关联")[:30]
        if tid in at:
            rels[at[tid]]["type"] = new_type
        else:
            rels.append({"target": tid, "type": new_type})
            at[tid] = len(rels) - 1
    card["relationships"] = rels
    # behavior_rules：并集去重截断上限
    rules = [str(x) for x in (card.get("behavior_rules") or []) if str(x).strip()]
    have_r = set(rules)
    for x in (proposal.get("behavior_rules") or []):
        s = str(x).strip()
        if s and s not in have_r:
            rules.append(s)
            have_r.add(s)
    card["behavior_rules"] = rules[:MAX_RULES]
    # age：仅当原卡缺时才补
    if card.get("age") is None and proposal.get("age") is not None:
        card["age"] = int(proposal["age"])
    # provenance：标记补喂来源（不覆盖已有 origin）
    card.setdefault("provenance", {})
    card["provenance"].setdefault("origin", "enrich")
    card["provenance"]["channel"] = "manual_confirm"
    card["provenance"]["confidence"] = CONFIDENCE
    return card


# ---------------------------------------------------------------- 提案


_PREAMBLE = (
    "你是人物设定师。为一张修仙小说人物卡补全缺失字段——relationships（与已登记角色的"
    "关系）与 behavior_rules（可执行的行为规则）。\n"
    "铁律：\n"
    "1. **不新增任何角色**——关系的 target 只能从【全员名册】中选（可用名或别名），"
    "漏写、错写都会被确定性闸门拒绝；\n"
    "2. **不与自己建立关系**；\n"
    "3. 关系与行为规则必须与【该角色已发生的经历】一致（不得与已发生事件矛盾），"
    "并且服务于长篇连贯性（为后续冲突/羁绊埋钩子）；\n"
    "4. behavior_rules 写**可执行的动作/立场规则**（2-3 条，每条一句），"
    "不写性格形容词（那是 core_traits）；\n"
    "5. age 只给人族/妖族等生物给合理年龄，非生物实体（系统/器灵）给 null。\n"
)


def propose(ws, project_id: str, provider, *, card_ids: list[str] | None = None):
    """对缺料卡批量提案：LLM 草提案 → 确定性闸门 → 写 pending（待人工确认）。

    - card_ids=None → 全部缺料卡；否则只处理指定 id（可按名字定位）。
    - 每卡返回 dict：{"card_id","ok","reasons":[],"proposed":bool}
      ok=True 且 proposed=True → 已入 pending；ok=False → reasons 披露拒绝原因。
    - LLM/解析失败该卡静默跳过（proposed=False, reasons 记录），不阻断其余卡。
    """
    chars = [c for c in (_read_bible_json(ws, project_id, "characters.json") or [])
             if isinstance(c, dict) and c.get("id")]
    if card_ids:
        chars = [c for c in chars if c.get("id") in card_ids]
    wv = _read_bible_json(ws, project_id, "worldview.json") or {}
    levels = "、".join(str(x) for x in ((wv.get("power_system") or {}).get("levels") or []))
    world_name = str(wv.get("name") or "")
    roster = _roster_block(ws, project_id)
    threads = _read_bible_json(ws, project_id, "plot_threads.json") or []
    thread_txt = "；".join(str(t.get("desc") or "") for t in threads[:6]) if threads else ""

    pending_path = ws._abs(f"{project_id}/bible/{ENRICH_PATH}")
    pending = json.loads(pending_path.read_text(encoding="utf-8")) if pending_path.exists() else []
    pending = pending if isinstance(pending, list) else []
    done_ids = {p.get("card_id") for p in pending if isinstance(p, dict)}

    results: list[dict] = []
    for card in chars:
        cid = str(card["id"])
        missing = needs_enrichment(card)
        if not missing:
            results.append({"card_id": cid, "name": card.get("name"), "ok": True,
                            "proposed": False, "reasons": ["无缺料，跳过"]})
            continue
        if cid in done_ids:
            results.append({"card_id": cid, "name": card.get("name"), "ok": True,
                            "proposed": False, "reasons": ["已在 pending，勿重复提案"]})
            continue
        if provider is None:
            results.append({"card_id": cid, "name": card.get("name"), "ok": False,
                            "proposed": False, "reasons": ["无可用 provider"]})
            continue
        res = _propose_one(card, provider, ws=ws, project_id=project_id,
                           world_name=world_name, levels=levels, roster=roster,
                           thread_txt=thread_txt)
        if not res["ok"]:
            results.append(res)
            continue
        proposal = res["proposal"]
        reasons = check_proposal(card, proposal, ws=ws, project_id=project_id)
        # A3 语义闸门：账本冲突并入拒绝原因；细纲前提冲突降级为备注（人工拍板）
        sem_reasons, sem_notes = semantic_gate(card, proposal, ws=ws,
                                               project_id=project_id)
        reasons = reasons + sem_reasons
        if reasons:
            res["ok"] = False
            res["proposed"] = False
            res["reasons"] = reasons
            results.append(res)
            continue
        pending.append({
            "card_id": cid,
            "card_name": str(card.get("name") or ""),
            "missing": missing,
            "age": proposal.get("age"),
            "relationships": proposal.get("relationships") or [],
            "behavior_rules": proposal.get("behavior_rules") or [],
            "notes": sem_notes,
            "reason": "P0-A 数据补喂：LLM 提案待人工确认（origin=enrich，确认后入档）",
        })
        results.append({"card_id": cid, "name": card.get("name"), "ok": True,
                        "proposed": True, "reasons": []})
    if any(r.get("proposed") for r in results):
        _write_bible_json(ws, project_id, ENRICH_PATH, pending)
    return results


def _propose_one(card: dict, provider, *, ws, project_id: str, world_name: str,
                 levels: str, roster: str, thread_txt: str) -> dict:
    """单卡 LLM 提案（不落盘）。"""
    cid = str(card["id"])
    prior = _prior_block(ws, project_id, cid)
    prompt = (
        f"{_PREAMBLE}"
        f"目标人物卡：{_card_line(card)}\n"
        f"世界：{world_name}；境界体系：{levels or '（无）'}\n"
        f"主线（可作钩子方向）：{thread_txt or '（无）'}\n\n"
        f"【全员名册】（target 只能出自这里，写名或别名即可）：\n{roster}\n\n"
        f"【该角色已发生的经历】\n{prior}\n\n"
        "输出单个 JSON 对象：\n"
        '{"name": "<目标卡名，原样返回>", "age": 数字或null,\n'
        ' "relationships": [{"target": "<名册中角色名>", "type": "<一句话关系，30字内>"}],\n'
        ' "behavior_rules": ["<可执行规则1>", "<可执行规则2>", "<可执行规则3>"]}\n'
        "只输出 JSON。"
    )
    try:
        res = provider.complete(LLMRequest(
            messages=[LLMMessage(role="user", content=prompt)],
            max_tokens_out=700, temperature=0.3, response_format="json_object",
            thinking=False))  # 生成类：补料提案生成，关思考
        if res.blocked or not (res.content or "").strip():
            return {"card_id": cid, "name": card.get("name"), "ok": False,
                    "proposed": False, "reasons": ["LLM 空响应/blocked"]}
        data = json.loads(res.content.strip())
        if not isinstance(data, dict):
            raise ValueError("提案不是 JSON 对象")
        return {"card_id": cid, "name": card.get("name"), "ok": True,
                "proposed": False, "proposal": data, "reasons": []}
    except Exception as exc:  # noqa: BLE001 - LLM/解析失败该卡跳过，不阻断其余卡
        return {"card_id": cid, "name": card.get("name"), "ok": False,
                "proposed": False, "reasons": [f"LLM 提案失败：{exc}"]}


# ---------------------------------------------------------------- 人工确认（settings-pending 同款）


def load_pending(ws, project_id: str) -> list[dict]:
    p = ws._abs(f"{project_id}/bible/{ENRICH_PATH}")
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    return data if isinstance(data, list) else []


def list_pending(ws, project_id: str) -> list[str]:
    """pending 摘要行（供 CLI 无参审阅）。"""
    out: list[str] = []
    chars = {c.get("id"): c for c in (_read_bible_json(ws, project_id, "characters.json") or [])}
    for p in load_pending(ws, project_id):
        rels = "；".join(f"{r.get('target')}·{r.get('type')}" for r in (p.get("relationships") or []))
        rules = "；".join(str(x) for x in (p.get("behavior_rules") or []))
        out.append(f"· {p.get('card_name')}（{p.get('card_id')}）缺 {p.get('missing')}")
        if p.get("age") is not None:
            out.append(f"    age: {p['age']}")
        if rels:
            out.append(f"    relationships: {rels}")
        if rules:
            out.append(f"    behavior_rules: {rules}")
        for note in (p.get("notes") or []):   # A3 语义闸门备注（细纲前提留意）
            out.append(f"    ⚠ {note}")
        chars.pop(p.get("card_id"), None)   # 已在 pending 的卡不用再提示
    return out


def apply_pending(ws, project_id: str, *, allow: set[str] | None = None,
                  deny: set[str] | None = None) -> tuple[int, int, int]:
    """按卡名 allow/deny 合并 pending 进 characters.json。返回 (moved, dropped, remaining)。"""
    allow = allow or set()
    deny = deny or set()
    pending = load_pending(ws, project_id)
    chars = [c for c in (_read_bible_json(ws, project_id, "characters.json") or [])
             if isinstance(c, dict)]
    by_id = {c.get("id"): c for c in chars}
    kept, moved, dropped = [], 0, 0
    changed = False
    for p in pending:
        name = str(p.get("card_name") or "")
        if name in allow:
            card = by_id.get(p.get("card_id"))
            if card is not None:
                _apply_one_card(card, {
                    "name": name,
                    "age": p.get("age"),
                    "relationships": p.get("relationships") or [],
                    "behavior_rules": p.get("behavior_rules") or [],
                }, ws=ws, project_id=project_id)
                changed = True
                moved += 1
                continue
            kept.append(p)   # 卡已不在名册（异常态）→ 保留待人工处理
        elif name in deny:
            dropped += 1
        else:
            kept.append(p)
    if moved and changed:
        _write_bible_json(ws, project_id, "characters.json", chars)
    if moved or dropped:
        _write_bible_json(ws, project_id, ENRICH_PATH, kept)
    return moved, dropped, len(kept)
