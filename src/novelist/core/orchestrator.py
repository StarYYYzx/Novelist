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

from .session import Budget, SessionInfo
from .writeback import LandedEvent, commit_event


class ProductionResult:
    def __init__(self, ok: bool, chapter_path: str | None = None, result: str = "", events_committed: int = 0,
                 mode: str = "tool", *, bible_injected: bool = False, attempts: int = 1,
                 completeness: dict | None = None, polish=None, chronicle=None):
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


def _completeness_problems(comp: dict) -> list[str]:
    problems = []
    if not comp.get("ends_properly"):
        problems.append(f"结尾被截断（末字「{comp.get('last_char', '')}」不是句末标点）")
    if comp.get("meta_narration"):
        problems.append("正文出现元叙事表述：" + "、".join(comp["meta_narration"]))
    return problems


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
    from .context import build_chapter_context
    from .polish import completeness, polish_chapter
    from ..core.llm import LLMMessage, LLMRequest

    sess = session or SessionInfo(project_id=project_id, agent="orchestrator")

    # ---- 1) 圣经注入（B-02）----
    bible_injected = False
    if system_prompt is None and final_goal is None and inject_bible:
        try:
            ctx = build_chapter_context(ws, project_id, vol, ch, memories=memories, genre=genre)
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

    # ---- 2) 生成（含完整性校验与重试，B-04）----
    mode = "tool"
    attempts = 0
    comp: dict = {}
    try:
        if prefer_direct:
            last_problems: list[str] = []
            for attempt in range(max_retries + 1):
                attempts = attempt + 1
                prompt = goal + (_REPAIR_HINT.format(problems="；".join(last_problems))
                                 if last_problems else "")
                res = provider.complete(
                    LLMRequest(
                        messages=[LLMMessage(role="system", content=system_prompt or ""),
                                  LLMMessage(role="user", content=prompt)],
                        max_tokens_out=generation_tokens,
                    )
                )
                final = res.content or ""
                if len(final) < direct_words_floor:
                    raise RuntimeError(f"direct generation too short ({len(final)} chars)")
                comp = completeness(final)
                problems = _completeness_problems(comp) if validate else []
                if not problems:
                    break
                last_problems = problems
                if attempt == max_retries:
                    comp["_unresolved"] = problems  # 重试耗尽：保留问题标记，不静默
            mode = "direct"
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

    # ---- 5) 事件回写（真实事件走编纂员，B-03）----
    events = 0
    chronicle = None
    try:
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
                            completeness=comp, polish=polish_res, chronicle=chronicle)
