"""主编剧编排流（docs/04 §4.1 / docs/05 §2）。

正文严格串行逐章：编排器发起 → 主编剧产出草稿 → 事件实时回写（ADR-013）→ 一章完成。

## 本模块承担的四项质量保障

1. **圣经注入（B-02）** —— `inject_bible=True` 时按章节装配 世界观 / 文风 / 出场人物卡 /
   主角硬约束 / 输出纪律 进 system prompt。此前圣经从不进入生成上下文，导致性别漂移、
   凭空造人、卷名漂移。
2. **生成完整性校验与自动重试（B-04）** —— 直出模式检测截断、元叙事泄漏、篇幅不足，
   失败则带修复提示重生成（此前只判断 `len(final) >= 20`，截断章节照单全收）。
3. **记忆编纂（B-03）** —— 成章后经 `Chronicler` 抽取真实情节事件并走冲突双检入库，
   取代原先只有「完成第 X 卷第 Y 章」的章级合成事件。
4. **文风润色（可选）** —— `polish=True` 时在成章之后**额外追加一次** LLM 调用专门优化
   文风，并用确定性指标复核：分数没变好就保留原文。

本地/慢 token 模型降级（ADR-006）：若模型直接返回正文文本而非工具调用
（本地小模型 tool_calling 常不可靠），把返回文本直接落盘为草稿，跳过工具往返。
"""

from __future__ import annotations

import re

from .llm import LLMMessage, LLMRequest
from .session import Budget, SessionInfo
from .writeback import LandedEvent, commit_event


class ProductionResult:
    def __init__(self, ok: bool, chapter_path: str | None = None, result: str = "", events_committed: int = 0,
                 mode: str = "tool", *, bible_injected: bool = False, attempts: int = 1,
                 completeness: dict | None = None, polish=None, chronicle=None,
                 review_blocks: int = 0, events_revised: int = 0, lessons_added: int = 0):
        self.ok = ok
        self.chapter_path = chapter_path
        self.result = result
        self.events_committed = events_committed
        self.mode = mode  # "tool"（工具调用写草稿） | "direct"（文本直接落盘）
        self.bible_injected = bible_injected
        self.attempts = attempts
        self.completeness = completeness or {}
        self.polish = polish            # core.polish.PolishResult | None
        self.chronicle = chronicle      # core.chronicler.ChroniclerReport | None
        self.review_blocks = review_blocks      # 事件级审校 block 数（讨论第 7 轮）
        self.events_revised = events_revised    # 因 block 重写的事件数
        self.lessons_added = lessons_added      # 本轮沉淀的历史教训数

    @property
    def ai_tone_before(self) -> float | None:
        return getattr(self.polish, "before", None) and self.polish.before.score

    @property
    def ai_tone_after(self) -> float | None:
        return getattr(self.polish, "after", None) and self.polish.after.score


_REPAIR_HINT = (
    "\n\n【上一稿不合格，必须修正】\n"
    "上一稿的问题：{problems}\n"
    "请重新输出完整的一章：写完细纲全部要点，结尾必须是一个完整的收束句"
    "（以句号/问号/感叹号/引号结束），正文里不得出现「第X章」「本章」等元叙事表述。"
)

# 每事件审校修订（讨论第 7 轮）：block 级问题带审校建议重写该事件，只修订不整章重来
_REVISE_HINT = (
    "\n\n【本事件未通过审校，必须修订后重写】\n"
    "审校问题（block 级，必须逐条修正）：\n{problems}\n"
    "请重写本事件：逐条修正上述问题；保留未出问题的情节、人物与对白；"
    "结尾必须是完整的收束句。"
)


def _completeness_problems(comp: dict) -> list[str]:
    problems = []
    if not comp.get("ends_properly"):
        problems.append(f"结尾被截断（末字「{comp.get('last_char', '')}」不是句末标点）")
    if comp.get("meta_narration"):
        problems.append("正文出现元叙事表述：" + "、".join(comp["meta_narration"]))
    return problems


_SEAM_CHARS = 300  # 事件间接缝：取上一事件末尾若干字，让模型自然续写

_SCRIPT_INSTRUCTION = ("""

【本轮特殊要求：剧本体】
不要写小说正文，改以剧本体输出本章的对白与动作交锋，格式为：
角色名：（动作/神态）台词
- 只写对白与关键动作，不写环境描写与心理旁白
- 覆盖本章细纲全部要点，冲突要给足
- 人物说话必须贴合各自人设，声音要有区分度
""")


def _generate_with_continuation(provider, system_prompt: str, prompt: str,
                                budget: int, max_continuations: int) -> str:
    """生成一次；若被 length 截断则**续写**而不是整章重来（第二批第 1 条·第 2 层修复）。

    重来一遍要重新付思考开销，且已生成的好内容可能被换掉；续写把已有文本作为前缀，
    只补齐剩余部分。返回拼接后的完整文本。
    """
    res = provider.complete(
        LLMRequest(messages=[LLMMessage(role="system", content=system_prompt),
                             LLMMessage(role="user", content=prompt)],
                   max_tokens_out=budget)
    )
    text = res.content or ""
    for _ in range(max(0, max_continuations)):
        if res.finish_reason != "length" or not text:
            break
        res = provider.complete(
            LLMRequest(messages=[
                LLMMessage(role="system", content=system_prompt),
                LLMMessage(role="user", content=(
                    "接续下文继续写。从中断处自然写下去，不要重复已有内容，"
                    "不要总结，直到写完一个完整的收束句为止。\n\n"
                    "已写部分：\n" + text[-1500:])),
            ],
                max_tokens_out=budget)
        )
        piece = (res.content or "").strip()
        if not piece:
            break
        text = text.rstrip() + piece
    return text


def _recall_for(ws, project_id: str, query: str, embedding, top_k: int = 4) -> list[str]:
    """事件级先忆（检索是本地操作，不花 LLM 调用）。失败静默返回空。"""
    try:
        from .memory import MemoryIndex, MemoryQuery, MemoryRetriever

        idx = MemoryIndex.load(ws, project_id)
        if not idx.fragments:
            idx.rebuild(ws, project_id, embedding)
        if not idx.fragments:
            return []
        hits = MemoryRetriever(idx, embedding=embedding).query(
            MemoryQuery(query=query, top_k=top_k))
        return [f"- [{h.kind} @ {h.source.get('vol')}:{h.source.get('ch')}] {h.text}"
                for h in hits if h.score > 0]
    except Exception:  # noqa: BLE001
        return []


def _event_goal(chapter_goal: str, ev_text: str, idx: int, total: int, prev_piece: str,
                seam_chars: int, memories: list[str], is_last: bool,
                setting_lines: list[str] | None = None) -> str:
    """装配单个事件的生成目标（细纲要点 + 接缝上下文 + 事件级先忆 + 待交代设定）。"""
    parts = [f"{chapter_goal}", "",
             f"【本步骤】只撰写本章第 {idx}/{total} 个事件：【{ev_text}】"]
    if prev_piece:
        parts += ["", f"【上文接缝】（从下面这段的结尾自然续写，不要重复已有内容）：",
                  "…" + prev_piece[-seam_chars:]]
    if memories:
        parts += ["", "【相关前情】（先忆，保持一致）：", *memories]
    if setting_lines:
        parts += ["", "【本事件首次出现的设定】（以下设定此前未在正文交代过，"
                      "必须在本次事件里自然带出，让读者第一次见到就明白：）", *setting_lines]
    parts += ["", "篇幅约 300–600 字。" + ("这是本章最后一个事件，结尾必须是一个完整的收束句。"
                                         if is_last else
                                         "不要写本章其他事件的内容，写到本事件结束即停。")]
    return "\n".join(parts)


def _make_chronicler(ws, project_id: str, provider, embedding, semantic_checker):
    try:
        from .chronicler import Chronicler

        return Chronicler(ws, project_id, llm=provider, embedding=embedding,
                          semantic_checker=semantic_checker)
    except Exception:  # noqa: BLE001
        return None


def _load_settings(ws, project_id: str):
    """加载设定条目库；文件不存在返回 None（不启用首次交代状态机）。"""
    try:
        from .settings import SettingIndex

        idx = SettingIndex.load(ws, project_id)
        return idx if idx.entries else None
    except Exception:  # noqa: BLE001
        return None


def _make_reviewer(ws, project_id: str, provider):
    """构造审校师（LLM 语义检）；失败返回 None（不阻断生成）。"""
    try:
        from ..consistency.reviewer import Reviewer

        return Reviewer(ws, project_id, llm=provider)
    except Exception:  # noqa: BLE001
        return None


_LESSONS_MAX = 30  # review_lessons.json 上限，防无限膨胀


def _lesson_lines(ws, project_id: str) -> list[str]:
    """读历史教训（bible/review_lessons.json）→ 注入行。"""
    try:
        p = ws._abs(f"{project_id}/bible/review_lessons.json")
        if not p.exists():
            return []
        import json as _json

        data = _json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    lines = []
    for it in data if isinstance(data, list) else []:
        if not isinstance(it, dict):
            continue
        rule = str(it.get("rule") or it.get("issue") or "").strip()
        if not rule:
            continue
        if len(rule) > 80:
            rule = rule[:77] + "…"
        lines.append(f"- [{it.get('category', '审校')}] {rule}")
    return lines


def _append_lessons(ws, project_id: str, issues, vol: int, ch: int) -> int:
    """把 block 级审校问题沉淀为历史教训（项目级独立文件，讨论第 7 轮）。

    返回新增条数。去重键：category + detail 前 40 字。
    """
    blocks = [i for i in issues if getattr(i, "level", "") == "block"]
    if not blocks:
        return 0
    import json as _json

    p = ws._abs(f"{project_id}/bible/review_lessons.json")
    try:
        data = _json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    except (ValueError, OSError):
        data = []
    if not isinstance(data, list):
        data = []
    added = 0
    for i in blocks:
        key = f"{i.category}|{i.detail[:40]}"
        if any(str(x.get("_key", "")) == key for x in data):
            continue
        rule = (i.suggestion or i.detail).strip()
        data.append({
            "_key": key,
            "category": i.category,
            "issue": i.detail[:120],
            "rule": rule[:200],
            "source": f"{vol}:{ch}",
        })
        added += 1
    if added:
        data = data[-_LESSONS_MAX:]
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:  # pragma: no cover
            return 0
    return added


def _merge_reports(reports: list):
    """合并多份编纂报告（事件循环每事件一份）。"""
    from .chronicler import ChroniclerReport

    merged = ChroniclerReport()
    for r in reports:
        merged.extracted += getattr(r, "extracted", 0)
        merged.written += getattr(r, "written", 0)
        merged.conflicts.extend(getattr(r, "conflicts", []) or [])
        merged.events.extend(getattr(r, "events", []) or [])
        merged.threads_activated += getattr(r, "threads_activated", 0)
        for cid, delta in (getattr(r, "state_updates", {}) or {}).items():
            merged.state_updates.setdefault(cid, {}).update(delta)
    return merged


_TITLE_RE = re.compile(r"^#{1,6}\s*第\s*[一二三四五六七八九十\d]+\s*章")


def _dedupe_chapter_titles(text: str) -> str:
    """事件循环拼接后处理：模型常在每个事件开头重写章节标题，删除非首行重复标题。

    实测（诡夜大学 ch1）：3 事件拼出 4065 字，第 2 个事件开头又写了一遍
    「## 第 1 章 脚步声在午夜响起」——读者视角是标题重复。
    """
    kept: list[str] = []
    title_seen = False
    for ln in text.splitlines():
        if _TITLE_RE.match(ln.strip()):
            if title_seen:
                continue
            title_seen = True
        kept.append(ln)
    return "\n".join(kept).strip("\n")


def produce_chapter(
    ws,
    project_id: str,
    vol: int,
    ch: int,
    provider,
    *,
    session: SessionInfo | None = None,
    registry=None,
    system_prompt: str | None = None,
    goal_prefix: str = "请撰写并输出下一章正文。",
    final_goal: str | None = None,
    max_rounds: int = 30,
    direct_words_floor: int = 20,
    prefer_direct: bool = False,
    generation_tokens: int = 4000,
    embedding=None,
    semantic_checker=None,
    # ---- 新增：质量保障开关 ----
    inject_bible: bool = True,
    genre: str | None = None,
    memories: list[str] | None = None,
    validate: bool = True,
    max_retries: int = 1,
    commit_chapter_event: bool | None = None,
    chronicler=None,
    polish: bool = False,
    # ---- 新增：事件循环 / 续写 / 剧本草稿（人工审查第二、三批）----
    event_loop: bool = False,
    screenplay: bool = False,
    max_continuations: int = 2,
    settings=None,
    event_review: bool = True,   # 每事件审校+修订（讨论第 7 轮）；仅事件循环生效
) -> ProductionResult:
    """主编剧驱动产出第 vol 卷 ch 章草稿（严格串行，同时至多一章）。

    - `inject_bible`：按章节装配圣经进 system prompt（B-02）。显式传 `system_prompt` 时不覆盖。
    - `validate` / `max_retries`：生成完整性校验与自动重试（B-04）。**仅对直出模式生效**——
      工具模式下正文由 write_draft 工具落盘，重试会重复消耗脚本化的 provider。
    - `chronicler`：传入 `Chronicler` 实例即用真实事件抽取；为 None 且 `provider` 可用时自动构造。
    - `polish`：成章后追加一次 LLM 调用优化文风（默认关，成本较高）。
    - `commit_chapter_event`：是否写入章级合成事件「完成第 X 卷第 Y 章」。
      `None`（默认）= **自动兜底**：编纂员写出了真实情节事件就不写合成事件，
      编纂不可用 / 一条都没写出来时才回退写入，保证章节进度至少留痕。
    """
    from .context import build_chapter_context, parse_key_events
    from .polish import completeness, polish_chapter

    sess = session or SessionInfo(project_id=project_id, agent="orchestrator")

    # ---- 1) 圣经注入（B-02）----
    bible_injected = False
    if system_prompt is None and final_goal is None and inject_bible:
        try:
            # 历史教训（讨论第 7 轮）：此前审校 block 沉淀的纪律，随圣经注入
            lesson_lines = _lesson_lines(ws, project_id)
            ctx = build_chapter_context(ws, project_id, vol, ch, memories=memories, genre=genre,
                                        lessons=lesson_lines or None)
            system_prompt, final_goal = ctx.system_prompt, ctx.user_goal
            bible_injected = True
        except Exception:  # noqa: BLE001 - 工作区不全时退回默认提示，不阻断写章
            system_prompt = None
    elif system_prompt is None:
        system_prompt = "你是主编剧，负责指挥创作。"

    if final_goal is None:
        goal = f"{goal_prefix}：第 {vol} 卷第 {ch} 章（project={project_id}）"
    else:
        goal = final_goal

    # 声明式事件清单（第二批第 2 条）：key_events 写在细纲 front-matter，生成期直接迭代
    gist_text_for_events = ""
    try:
        _gist_p = ws.outline_chapter_path(project_id, vol, ch)
        if _gist_p.exists():
            gist_text_for_events = _gist_p.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        gist_text_for_events = ""

    # ---- 2) 生成（事件循环 / 剧本草稿 / 完整性校验与续写，B-04 + 第二、三批讨论）----
    mode = "tool"
    attempts = 0
    comp: dict = {}
    chronic_reports: list = []
    review_blocks = 0       # 事件级审校 block 数（讨论第 7 轮）
    events_revised = 0      # 因 block 重写的事件数
    lessons_added = 0       # 本轮沉淀的历史教训数
    try:
        if prefer_direct:
            mode = "direct"
            final = ""

            if screenplay:
                # 剧本草稿（第三批第 2 条·档 2）：先剧本体写对白交锋，再叙事化。
                # 多声音质感 ↑，成本 ×2，无失控风险；重场戏专用。
                script_res = provider.complete(
                    LLMRequest(
                        messages=[LLMMessage(role="system", content=system_prompt or ""),
                                  LLMMessage(role="user", content=goal + _SCRIPT_INSTRUCTION)],
                        max_tokens_out=generation_tokens,
                    )
                )
                script_text = (script_res.content or "").strip()
                if script_text:
                    goal = (goal + "\n\n【对白草稿】（仅作素材：保留其中对白原话与冲突走向，"
                            "转化为小说叙事，补足动作、场景与心理描写，不要保留剧本格式）\n"
                            + script_text)

            key_events = parse_key_events(gist_text_for_events) if event_loop else []
            if key_events:
                # 事件循环（第二批第 2 条）：按声明式 key_events 逐事件推进、逐事件回写。
                # 这是 ADR-013「事件落定即回写」的落地——章内后续事件可先忆到上一事件。
                pieces: list[str] = []
                seam = max(200, min(400, _SEAM_CHARS))
                settings_idx = (settings if settings is not None
                                else _load_settings(ws, project_id))
                for idx, ev_text in enumerate(key_events, 1):
                    is_last = idx == len(key_events)
                    memories_ev = _recall_for(ws, project_id, ev_text, embedding)
                    # 设定按需注入：本事件命中且未交代的条目（首次交代状态机，讨论决策）
                    setting_lines = (settings_idx.pending_lines(goal, ev_text, ch=ch)
                                     if settings_idx is not None else [])
                    prompt = _event_goal(goal, ev_text, idx, len(key_events),
                                         pieces[-1] if pieces else "", seam,
                                         memories_ev, is_last, setting_lines)
                    piece = _generate_with_continuation(
                        provider, system_prompt or "", prompt, generation_tokens,
                        max_continuations)
                    if len(piece) < direct_words_floor:
                        raise RuntimeError(
                            f"event {idx}/{len(key_events)} too short ({len(piece)} chars)")

                    # 每事件审校 + 修订（讨论第 7 轮）：block → 带建议重写 1 次。
                    # 审校只读产出工单，修订由编排层决定（docs/04 §5.4 双层门禁语义层）。
                    if event_review:
                        reviewer = _make_reviewer(ws, project_id, provider)
                        if reviewer is not None:
                            try:
                                issues = reviewer.review(
                                    piece, vol, ch, gist_text=gist_text_for_events,
                                    memories=memories_ev)
                                blocks = [i for i in issues if i.level == "block"]
                            except Exception:  # noqa: BLE001 - 审校失败不阻断
                                issues, blocks = [], []
                            if blocks:
                                review_blocks += len(blocks)
                                problems = "\n".join(
                                    f"- [{i.category}] {i.detail} 修订建议：{i.suggestion or '按维度修正'}"
                                    for i in blocks)
                                revised = _generate_with_continuation(
                                    provider, system_prompt or "",
                                    prompt + _REVISE_HINT.format(problems=problems),
                                    generation_tokens, max_continuations)
                                if len(revised) >= direct_words_floor:
                                    piece = revised
                                    events_revised += 1
                                lessons_added += _append_lessons(ws, project_id, blocks, vol, ch)

                    pieces.append(piece)
                    attempts += 1
                    # 逐事件回写（ADR-013）；失败不阻断，交由章级兜底
                    if chronicler is None:
                        chronicler = _make_chronicler(ws, project_id, provider, embedding,
                                                      semantic_checker)
                    if chronicler is not None:
                        try:
                            chronic_reports.append(
                                chronicler.run(piece, vol, ch, max_events=2,
                                               tag=f"e{idx}"))
                        except Exception:  # noqa: BLE001 - 单事件编纂失败不阻断整章
                            pass
                final = _dedupe_chapter_titles("\n".join(pieces))
            else:
                last_problems: list[str] = []
                for attempt in range(max_retries + 1):
                    attempts = attempt + 1
                    prompt = goal + (_REPAIR_HINT.format(problems="；".join(last_problems))
                                     if last_problems else "")
                    final = _generate_with_continuation(
                        provider, system_prompt or "", prompt, generation_tokens,
                        max_continuations)
                    if len(final) < direct_words_floor:
                        raise RuntimeError(f"direct generation too short ({len(final)} chars)")
                    comp = completeness(final)
                    problems = _completeness_problems(comp) if validate else []
                    if not problems:
                        break
                    last_problems = problems
                    if attempt == max_retries:
                        comp["_unresolved"] = problems  # 重试耗尽：保留问题标记，不静默
        else:
            from .agent_runner import AgentRunner

            reg = registry or None
            if reg is None:
                from ..tools import build_registry

                reg = build_registry(ws, embedding=embedding)
            runner = AgentRunner(provider, sess,
                                 budget=Budget(max_tokens_out=generation_tokens, max_rounds=max_rounds),
                                 registry=reg)
            runner.system(system_prompt or "")
            final = runner.run_loop(goal, max_rounds=max_rounds)
            attempts = 1
            # 工具模式：正文由 write_draft 落盘，完整性以草稿文件为准（不重试）
            if validate and len(final) >= direct_words_floor:
                comp = completeness(final)
    except Exception as e:  # noqa: BLE001 - 循环/生成异常统一收敛为失败
        return ProductionResult(ok=False, result=str(e), mode=mode, bible_injected=bible_injected,
                                attempts=attempts, completeness=comp)

    if not comp and final:
        comp = completeness(final)

    # ---- 3) 落盘草稿 ----
    draft = ws.draft_path(project_id, vol, ch)
    if mode == "direct":
        try:
            draft.parent.mkdir(parents=True, exist_ok=True)
            ws.write_text(draft, final + "\n")
        except Exception:  # noqa: BLE001
            return ProductionResult(ok=False, result="cannot write draft", mode=mode,
                                    bible_injected=bible_injected, attempts=attempts, completeness=comp)

    if not comp:
        try:
            comp = completeness(draft.read_text(encoding="utf-8")) if draft.exists() else {}
        except OSError:  # pragma: no cover
            comp = {}

    # ---- 4) 文风润色（成章后额外一次 LLM 调用）----
    polish_res = None
    if polish and mode == "direct":
        try:
            polish_res = polish_chapter(final, provider, vol=vol, ch=ch)
            if polish_res.changed:
                final = polish_res.text
                try:
                    ws.write_text(draft, final + "\n")
                except OSError:  # pragma: no cover
                    polish_res = None
        except Exception:  # noqa: BLE001 - 润色失败不阻断，保留原稿
            polish_res = None

    # ---- 4.5) 设定交代验证（首次交代状态机，讨论决策）----
    # 扫描成稿正文，命中关键词的未交代条目置 revealed=true 并写回；
    # 未命中的保留 false，下一章继续注入。
    settings_revealed = 0
    try:
        settings_idx = settings if settings is not None else _load_settings(ws, project_id)
        if settings_idx is not None:
            verify_text = draft.read_text(encoding="utf-8") if draft.exists() else final
            settings_revealed = len(settings_idx.verify(verify_text))
            if settings_revealed:
                settings_idx.save()
    except Exception:  # noqa: BLE001 - 交代验证失败不影响成稿
        settings_revealed = 0

    # ---- 5) 事件回写（真实事件走编纂员，B-03）----
    events = 0
    chronicle = None
    try:
        if chronic_reports:
            # 事件循环已在生成期逐事件回写（ADR-013），此处只合并报告，**不得**对整章重复编纂
            chronicle = _merge_reports(chronic_reports)
            events = chronicle.written
        else:
            text_for_chronicle = final if mode == "direct" else (
                draft.read_text(encoding="utf-8") if draft.exists() else "")
            if text_for_chronicle:
                if chronicler is None and inject_bible is not False:
                    from .chronicler import Chronicler

                    chronicler = Chronicler(ws, project_id, llm=provider, embedding=embedding,
                                            semantic_checker=semantic_checker)
                if chronicler is not None:
                    chronicle = chronicler.run(text_for_chronicle, vol, ch)
                    events = chronicle.written
    except Exception:  # noqa: BLE001 - 编纂失败不影响草稿已落盘
        chronicle = None

    if commit_chapter_event is None:
        # 自动兜底：有真实情节事件就不写合成事件，避免内容为空的条目污染 plot_events
        commit_chapter_event = events == 0
    if commit_chapter_event:
        ev = LandedEvent(
            project_id=project_id, vol=vol, ch=ch, seq=1,
            kind="chapter", summary=f"完成第 {vol} 卷第 {ch} 章",
        )
        try:
            commit_event(ev, sess, ws=ws, embedding=embedding, semantic_checker=semantic_checker)
            events += 1
        except Exception:  # noqa: BLE001 - 回写失败不影响本书草稿已落盘
            pass

    return ProductionResult(ok=True, chapter_path=str(draft), result=final, events_committed=events,
                            mode=mode, bible_injected=bible_injected, attempts=attempts,
                            completeness=comp, polish=polish_res, chronicle=chronicle,
                            review_blocks=review_blocks, events_revised=events_revised,
                            lessons_added=lessons_added)
