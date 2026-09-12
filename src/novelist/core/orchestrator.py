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

import os
import re

from .llm import LLMMessage, LLMRequest
from .session import Budget, SessionInfo
from .writeback import LandedEvent, commit_event


def _env_int(name: str, default: int) -> int:
    """读整型环境变量（H10：prompt 预算等无 CLI 通路的开关）。非法值回退默认。"""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class ProductionResult:
    def __init__(self, ok: bool, chapter_path: str | None = None, result: str = "", events_committed: int = 0,
                 mode: str = "tool", *, bible_injected: bool = False, attempts: int = 1,
                 completeness: dict | None = None, polish=None, chronicle=None,
                 review_blocks: int = 0, events_revised: int = 0, lessons_added: int = 0,
                 jit_added: int = 0, settings_pending: int = 0, factory_added: int = 0,
                 entity_new: int = 0, entity_alerts: list[str] | None = None,
                 phase: str = "writing", phase_reason: str = "",
                 length_truncated: bool = False, events_capped: int = 0,
                 first_seen_patched: int = 0,
                 volume_facts_built: bool = False,
                 seam_hits: int = 0,
                 pending_tick: dict | None = None,
                 chapter_title: str = "", directions_built: int = 0,
                 perspectives_written: int = 0, broadcasts_built: int = 0,
                 rel_pairs: int = 0, rel_proposals: int = 0,
                 soft_failures: list[str] | None = None):
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
        self.jit_added = jit_added              # 本轮 JIT 补卡数（递归分层 A）
        self.settings_pending = settings_pending  # 本轮设定待确认数（P0-B 闸门，settings_pending.json）
        self.factory_added = factory_added  # 本轮角色工厂注册数（ADR-022，含队列消化）
        self.entity_new = entity_new            # 本轮新增实体数（统一实体追踪）
        self.entity_alerts = entity_alerts or []  # 本轮实体预算告警
        self.phase = phase                      # 本卷阶段：opening | writing | tail（第九批）
        self.phase_reason = phase_reason        # 阶段判定依据（确定性规则可解释）
        self.length_truncated = length_truncated  # 遗留字段（机制已移除，恒 False）
        self.events_capped = events_capped        # 每章事件数超限截掉的事件数（第二批）
        self.first_seen_patched = first_seen_patched  # 首登场身份局部重写段数（方案 B）
        self.volume_facts_built = volume_facts_built  # 卷末事实清单是否已产（批次三·3）
        self.seam_hits = seam_hits                    # 接缝审查命中数（批次三·2）
        self.pending_tick = pending_tick or {}    # 定时事项记账（ADR-019，M3m T2）
        self.chapter_title = chapter_title        # 延迟拟定的章节标题（ADR-020 决策一）
        self.directions_built = directions_built  # 本轮生成的人物调度单数（ADR-020 决策三）
        self.perspectives_written = perspectives_written  # 本轮写入的角色视角条数（决策四）
        self.broadcasts_built = broadcasts_built  # 本轮广播成功次数（ADR-021 选角推理）
        self.rel_pairs = rel_pairs        # 章末实然关系账本 pair 数（ADR-023）
        self.rel_proposals = rel_proposals  # 阈值触发的翻转提案数（入 enrich pending）
        # H1 修复（2026-09-05）：增强层"静默吞异常"的可观测出口——接缝审查/回写/
        # 润色等失败与"检查通过"在结果上不可区分，这里显式留痕供 CLI/审计展示。
        self.soft_failures = soft_failures or []

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

# 接缝审查（批次三·方案2）：确定性 strip_seam_overlap 挡"字面复述"，
# 这里用 LLM 挡"换措辞重演同一情节"（ch1"母亲来电"写两遍的实证根因）。
_SEAM_WINDOW = 350       # 送审窗口：前片段尾 / 新片段头各取多少字
_SEAM_REPAIR_MAX = 2     # 每章最多修复次数（防预算风暴）
_SEAM_REJECT = (
    "\n\n【接缝复述，必须重写】\n上一片段刚写过：{what}\n"
    "你的新片段开头在**换措辞重演**这段内容（同名情节已发生，读者已看过）。\n"
    "要求：删除全部复述部分，直接从上一片段结束后的**新进展**起笔；"
    "不得重复任何已发生的动作、对白、消息；保留新片段中真正的新情节。"
)

# 事件模式章末修复（2026-09-04）：事件循环里每个片段单独看过都合格，拼起来才会
# 暴露截断/元叙事/残留重复——direct 模式有 problem→重试闭环（_REPAIR_HINT），
# 事件模式此前只有确定性去重、无修复通道。整章修复一次，问题减少才采纳。
_CHAPTER_REPAIR_HINT = (
    "\n\n【本章正文存在以下问题，必须修复】\n"
    "{problems}\n"
    "【修复纪律】只修复上述问题：删除重复段落与重复句、把截断的结尾补成完整的"
    "收束句、删除元叙事表述；不改动情节、人物、时间线与既有文风；"
    "直接输出修复后的完整正文，不要任何解释。"
)


def _repair_chapter_text(provider, system_prompt: str, text: str,
                         problems: list[str], generation_tokens: int) -> str:
    """章末问题清单 → 一次 LLM 修复调用。返回修复稿（失败/过短返回空串由调用方丢弃）。"""
    if not problems:
        return ""
    prompt = (text + "\n" + _CHAPTER_REPAIR_HINT.format(problems="；".join(problems)))
    try:
        return _generate_with_continuation(provider, system_prompt, prompt,
                                           generation_tokens, 0)
    except Exception:  # noqa: BLE001 - 修复失败不影响原稿
        return ""


def _completeness_problems(comp: dict) -> list[str]:
    problems = []
    if not comp.get("ends_properly"):
        problems.append(f"结尾被截断（末字「{comp.get('last_char', '')}」不是句末标点）")
    if comp.get("meta_narration"):
        problems.append("正文出现元叙事表述：" + "、".join(comp["meta_narration"]))
    # 重复（前两章归因 P0）：此前只查截断与元叙事，整段重复 4 次也 0 告警。
    # 阈值：整段重复 ≥1 或整句重复 ≥3 才算问题——单句重复 1-2 次可能是有意的复沓。
    if comp.get("dup_paragraphs", 0) >= 1:
        problems.append(f"正文有 {comp['dup_paragraphs']} 处整段重复，必须删除多余份")
    elif comp.get("dup_sentences", 0) >= 3:
        problems.append(f"正文有 {comp['dup_sentences']} 处整句重复，必须删除多余份")
    # 近重复（事件重演/结尾段复制）：ch7/ch8 实证——同一事件或同一收尾动作被写了两遍，
    # 措辞不同所以精确比对查不出（日志只报 1~2 处精确重复），人工审读才发现整段重演。
    if comp.get("dup_near_paragraphs", 0) >= 1:
        problems.append(
            f"正文有 {comp['dup_near_paragraphs']} 处近似重复段落"
            f"（同一事件或收尾动作被换措辞写了两遍），必须删掉后写的那一份，"
            f"只保留情节推进到最新状态的一次")
    return problems


# ---- 方案6.2（质量加固 2026-09-05）：首次出场身份线索检查 ----
_IDENTITY_CUE_RE = re.compile(
    "学姐|学长|师兄|师姐|师弟|师妹|导师|教授|老师|同学|室友|队长|副队|长老|弟子|"
    "掌门|馆主|医生|警察|记者|老板|管家|侍女|仆人|同事|上司|下属|修士|散修|"
    "邻居|发小|闺蜜|司长|局长|组长|队员|搭档|助手|秘书|夫人|少爷|小姐|先生|女士")
# 2026-09-05 收紧：剔除 孩子|学生|男子|女子|少年|少女|青年|中年人|老者 等泛词——
# 真机实证（proj-cloudb5 ch1）：苏清婉首现句"别吓唬孩子了"靠"孩子"误命中放行。


def _has_identity_cue(text: str, name: str, *, window: int = 260) -> bool:
    """名字首现处的邻近文本是否含身份/关系线索（启发式，供审校清单非硬闸）。"""
    i = text.find(name)
    if i < 0:
        return True  # 正文未字面点名（别名出场等）不判
    seg = text[max(0, i - 60): i + window]
    if _IDENTITY_CUE_RE.search(seg):
        return True
    pre = text[max(0, i - 30): i]
    if re.search(r"(名叫|叫做|名为|称为|自称)", pre):
        return True
    post = text[i + len(name): i + len(name) + 8]
    if re.match(r"(是|乃是|便是|原来是)", post):
        return True
    return False


def _first_appearance_problems(tracker, text: str) -> list[str]:
    """本章首次出场的角色正文无身份线索 → 审校问题（进修复调用清单）。

    问题3 真机归因：首登场只有一行软指令且无审校兜底——本检查补上"不过审校清单"
    这一环。tracker 为 None 或异常时静默跳过（不阻断生成）。
    """
    if tracker is None or not text:
        return []
    try:
        probs = []
        for e in tracker.resolve(text):
            if e.type != "character" or e.stage != "unseen" or e.first_ch:
                continue  # 只查"从未出场过"的角色（首现即本章）
            if tracker.is_core(e.key):
                continue  # 主角/核心的开篇交代由 D15 opening_rule 负责，不在此重复施压
            if not _has_identity_cue(text, e.name):
                probs.append(f"新角色「{e.name}」首次出场未交代身份/与主角的关系，"
                             f"须补一句自然介绍（通过行动或对白，不要人物简介式）")
        return probs
    except Exception:  # noqa: BLE001 - 检查失败不影响成稿
        return []


def _patch_first_appearances(ws, project_id: str, vol: int, ch: int, text: str,
                             provider, tracker, *, max_patches: int = 2,
                             min_reply_chars: int = 30) -> tuple[str, int]:
    """首登场身份补写（用户 2026-09-05 提议落地）：检测 → 定位首现段落 → LLM 仅重写该段。

    与"事后硬插一段简介"的区别：重写段落时要求把身份/关系交代**织入**原段的动作
    或对白，不新增段落、不动其他内容。返回 (new_text, patched_count)；任何失败
    静默返回原文（补写是增强，不是闸门）。
    """
    if tracker is None or not text:
        return text, 0
    try:
        probs = _first_appearance_problems(tracker, text)
        if not probs:
            return text, 0
        # 背景参考：bible/characters.json（sync 时已被 coerce 成干净短名）
        cards: dict[str, dict] = {}
        try:
            import json as _json

            p = ws._abs(f"{project_id}/bible/characters.json")  # noqa: SLF001
            if p.exists():
                for c in _json.loads(p.read_text(encoding="utf-8")):
                    if isinstance(c, dict) and c.get("name"):
                        cards[str(c["name"])] = c
        except Exception:  # noqa: BLE001
            cards = {}
        paras = text.split("\n\n")
        patched = 0
        for prob in probs:
            if patched >= max_patches:
                break
            m = re.search(r"「(.+?)」", prob)
            name = m.group(1) if m else ""
            if not name:
                continue
            idx = next((i for i, p_ in enumerate(paras) if name in p_), None)
            if idx is None:
                continue
            card = cards.get(name) or next(
                (c for n, c in cards.items() if n and (n in name or name in n)), {})
            bg = str(card.get("background") or "")[:120]
            before = paras[idx - 1][-80:] if idx > 0 else ""
            para = paras[idx]
            prompt = (
                "你是小说正文局部修订器。只重写下面这一段，不输出其他内容。\n"
                f"要求：把角色「{name}」的身份或与主角的关系交代自然织入本段"
                "（通过动作、称谓或对白，一句即可；禁止人物简介式罗列、禁止新增段落）。\n"
                f"角色背景参考：{bg or '（无，按上下文合理补写）'}\n"
                f"上一段末尾（仅供衔接语气参考，不要复述）：{before or '（无）'}\n"
                f"【原段】\n{para}\n"
                "输出：仅输出重写后的这一段本身。")
            try:
                res = provider.complete(LLMRequest(
                    messages=[LLMMessage(role="user", content=prompt)],
                    max_tokens_out=500, temperature=0.7,
                    thinking=False))  # 生成类：正文局部重写，关思考保预算
            except Exception:  # noqa: BLE001
                continue
            new_para = (res.content or "").strip() if getattr(res, "ok", False) else ""
            if len(new_para) < min_reply_chars or name not in new_para:
                continue  # 回复过短/丢名 → 视为无效补写（fake provider 也会在此被挡）
            paras[idx] = new_para
            patched += 1
        if not patched:
            return text, 0
        return "\n\n".join(paras), patched
    except Exception:  # noqa: BLE001 - 补写失败不影响成稿
        return text, 0


_SEAM_CHARS = 300  # 事件间接缝：取上一事件末尾若干字，让模型自然续写
# M3t（用户 2026-09-04 提议）：正文滑动窗口 + prompt 总预算。
# 窗口>接缝部分承担"让模型看见前文发生了什么"的反重演职责；预算默认关（0），
# 本地 9B 思考 token 两头夹击时建议 9000~12000，云端/DeepSeek 可放开或不设。
_PROSE_WINDOW_CHARS = 1200
# H10 修复（2026-09-05）：支持环境变量覆盖（原先硬编码 0 且无外部通路，M3t 预算器
# 是死代码——ch7 OOM 场景实际无防护）。NOVELIST_PROMPT_CHAR_BUDGET=9000~12000
# 供本地 9B 思考 token 两头夹击时启用；云端/DeepSeek 不设（0=关闭）。
_PROMPT_CHAR_BUDGET = _env_int("NOVELIST_PROMPT_CHAR_BUDGET", 0)

_SCRIPT_INSTRUCTION = ("""

【本轮特殊要求：剧本体】
不要写小说正文，改以剧本体输出本章的对白与动作交锋，格式为：
角色名：（动作/神态）台词
- 只写对白与关键动作，不写环境描写与心理旁白
- 覆盖本章细纲全部要点，冲突要给足
- 人物说话必须贴合各自人设，声音要有区分度
""")


# 句末标点后可跟右引号/右括号（中文引号 ” 常出现在句中，不能单独当切分点）
_SENT_RE = re.compile(r"[^。！？…]*[。！？…][”\"）】]*")


def _ngram_sim(a: str, b: str, n: int = 3) -> float:
    """`a` 的字符 n-gram 被 `b` 覆盖的比例（不对称）。

    **不用 Jaccard**：与长文本（上文接缝）比较时会被稀释——实测一句 54 字的复述
    对比 250 字上文，Jaccard 仅 0.16（上文大量无关 3-gram 撑大了并集），判不出重复。
    覆盖率（|A∩B| / |A|）才是"这句话有多少内容已在上文出现过"的正确度量。
    """
    if not a or not b:
        return 0.0
    A = {a[i:i + n] for i in range(len(a) - n + 1)}
    B = {b[i:i + n] for i in range(len(b) - n + 1)}
    if not A or not B:
        return 0.0
    return len(A & B) / len(A)


def _strip_punct(s: str) -> str:
    """去掉所有标点与空白（用于"去标点包含"判定）。"""
    return re.sub(r"[\s\W_]+", "", s or "")


def _seam_retell(provider, system_prompt: str, prev_tail: str, new_head: str) -> str:
    """接缝审查（批次三·方案2）：LLM 判断新片段开头是否在换措辞重演前片段刚写过的情节。

    返回复述描述（命中，用于 reject note 重生成）；空串 = 通过或审查失败（增强层，
    失败按通过处理，不阻断生成）。
    """
    prompt = (
        "你是小说连载的接缝审稿人。片段A是刚写完的上文结尾，片段B是新写的下一段开头。\n"
        "判断：B 的开头是否在**换措辞重演 A 已经写过的同一情节**（同一动作/同一消息/"
        "同一场景重新发生一遍）？正常的承接（呼应上文、反应上文）不算重演。\n"
        '只输出 JSON：{"retell": true/false, "what": "若重演，一句话概括被重演的情节"}\n\n'
        f"片段A（上文结尾）：\n…{prev_tail}\n\n"
        f"片段B（新片段开头）：\n{new_head}")
    try:
        res = provider.complete(LLMRequest(
            messages=[LLMMessage(role="user", content=prompt)],
            max_tokens_out=200, temperature=0.2,
            thinking=True))  # 判断类：接缝复述判定，开思考提 recall
        raw = (res.content or "").strip() if getattr(res, "ok", False) else ""
    except Exception:  # noqa: BLE001
        return ""
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return ""
    try:
        import json as _json

        data = _json.loads(m.group(0))
    except ValueError:
        return ""
    if isinstance(data, dict) and data.get("retell") is True:
        return str(data.get("what") or "上一片段刚写过的情节")
    return ""


# dp-seam：事件接缝从"原文末尾"改"结束状态锚"。原文接缝逼模型紧贴上一个事件的
# 措辞续写，与"禁复述"天然两难——而状态锚只给"事实"（此时谁在哪儿、处于什么状态、
# 局势如何），模型自然衔接却不必复述场景，缓解假性两难、降前后矛盾。
_END_STATE_PROMPT = (
    "你是小说的接缝记录员。下面是刚刚写完的一个事件的正文片段。\n"
    "请用**一句到两句、陈述式**的话，概括这一事件**结束时**的现场状态做锚点：\n"
    "- 当前所在的地点/场景；\n"
    "- 在场人物此刻各自的状态或动作（谁站着/负伤/正要离开/刚得知什么）；\n"
    "- 此时悬而未决的局势或氛围（如果有）。\n"
    "只写既成事实，不要抒情、不要推进剧情、不要写后续。直接输出状态，不要其他内容。\n\n"
    "事件：{ev}\n正文片段：\n…{body}"
)


def _event_end_state(provider, system_prompt: str, ev_text: str, piece: str) -> str | None:
    """为刚写完的事件产"结束状态锚"（dp-seam，开关 `orchestrator.seam_state`，默认关）。

    供下一个事件的接缝使用：用状态（事实）代替原文末尾（措辞），让事件自然衔接而
    不必复述上一个事件的场景。判断类调用，开思考。任一步失败返回 None → 调用方回退
    原文接缝（开关默认关→完全保持现有产出）。
    """
    try:
        res = provider.complete(LLMRequest(
            messages=[LLMMessage(role="system", content=system_prompt or ""),
                      LLMMessage(role="user", content=_END_STATE_PROMPT.format(
                          ev=_strip_expanded_tag(ev_text), body=piece[-800:]))],
            max_tokens_out=120, temperature=0.3,
            thinking=True))  # 判断类：接缝状态锚提取，开思考
        if res.blocked or not (res.content or "").strip():
            return None
        return " ".join(res.content.strip().split())
    except Exception:  # noqa: BLE001 - 增强层失败回退原文接缝，不阻断生成
        return None


def strip_seam_overlap(prev: str, piece: str, window: int = 900,
                       sim: float = 0.70, scan_sents: int = 40,
                       min_sent: int = 12, sim_first: float = 0.58) -> str:
    """去掉 `piece` 开头与 `prev` 尾部重复的片段（确定性去重）。

    **根因**：小模型拿到接缝上下文后，习惯性先把接缝**复述一遍**再续写
    ——实测 v5 ch1 事件 1 结尾与事件 2 开头重复整段（对白字符串完全一致），
    跨 v2→v5 一直存在（此前误记为"剧本草稿重复"）。prompt 里写了"不要重复"
    对 9B 无效，只能做确定性去重。

    策略：扫描开头的 `scan_sents` 句（默认 40 = 覆盖整个片段，见下），删掉与上文尾部
    重复（精确包含 / 去标点包含 / n-gram 相似 ≥ sim）的句子；**不要求连续**——实测重复
    常与新增信息交错（第 1 句纯复述、第 2 句含新信息、第 3 句又复述）。
    兜底：去重后过短（<30 字）保留原文。

    **前两章归因 P0 修订（2026-09-02）**：
    1. `scan_sents` 12 → 40。原值只扫开头 12 句，实测 ch1 的重复出现在第 13 句之后，
       函数直接放行；现在默认覆盖整个片段（事件片段通常 <25 句）。
    2. 新增 `sim_first=0.58`：开头 3 句是复述高发区（模型先"接住"上文），用更低的阈值。
       实测真实复述的 n-gram 相似度只有 ~0.29–0.6，统一 0.70 几乎全部漏判。
    3. 新增"去标点包含"判定：复述常改一两个标点/加个"道"，纯字符串包含会漏。
    """
    prev = (prev or "").strip()
    piece = (piece or "").strip()
    if not prev or not piece:
        return piece
    tail = prev[-window:]
    tail_raw = _strip_punct(tail)
    sents = [m.group(0) for m in _SENT_RE.finditer(piece) if m.group(0).strip()]
    rest_start = sum(len(s) for s in sents)
    tail_part = piece[rest_start:]
    if tail_part.strip():
        sents.append(tail_part)  # 末尾半句（未以句末标点结束）
    if not sents:
        return piece
    kept, dropped = [], 0
    for i, s in enumerate(sents):
        s_strip = s.strip()
        # 扫描范围内（默认全片段），其后内容一律保留（防误删正文）
        if i < scan_sents and dropped < scan_sents and len(s_strip) >= min_sent:
            threshold = sim_first if i < 3 else sim
            dup = (s_strip in tail) or (_ngram_sim(s_strip, tail) >= threshold)
            if not dup and len(s_strip) >= min_sent:
                dup = _strip_punct(s_strip) in tail_raw  # 只差标点/引号的复述
            if not dup and i == 0 and len(s_strip) >= 12:
                dup = s_strip[:12] in tail  # 首句可能是半句（无句末标点）
            if dup:
                dropped += 1
                continue
        kept.append(s)
    out = "".join(kept).strip()
    # 兜底：去重后过短（空/仅残片）→ 保留原文。**不用删除比例**——高复述片段
    # （实测 v5 ch1 事件 2 开头 80% 是复述）删掉 70% 恰恰是正确结果，按比例兜底会误拦。
    if len(out) < 30:
        return piece
    return out


def _generate_with_continuation(provider, system_prompt: str, prompt: str,
                                budget: int, max_continuations: int,
                                content_tokens: int | None = None) -> str:
    """生成一次；若被 length 截断则**续写**而不是整章重来（第二批第 1 条·第 2 层修复）。

    重来一遍要重新付思考开销，且已生成的好内容可能被换掉；续写把已有文本作为前缀，
    只补齐剩余部分。返回拼接后的完整文本。
    `content_tokens`：正文预算（第二批·人工审查），适配层保证总预算覆盖它。
    """
    res = provider.complete(
        LLMRequest(messages=[LLMMessage(role="system", content=system_prompt),
                             LLMMessage(role="user", content=prompt)],
                   max_tokens_out=budget, max_content_tokens=content_tokens,
                   thinking=False))  # 生成类：正文主体/续写，关思考保预算
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
                max_tokens_out=budget, max_content_tokens=content_tokens,
               thinking=False))  # 生成类：正文主体/续写，关思考保预算
        piece = (res.content or "").strip()
        if not piece:
            break
        # 续写同样会把已写部分复述一遍（同根因）——确定性去重后再拼接。
        # 注意：续写可能断在**句中**（"他走到山门前，正要/踏入那扇朱红大门。"），
        # 必须无缝拼接，不能加段落分隔——那是事件间 join（"\n\n".join(pieces)）的职责。
        text = text.rstrip() + strip_seam_overlap(text, piece).lstrip()
    return text


def _recall_for(ws, project_id: str, query: str, embedding, top_k: int = 4,
                exclude_src: tuple[int, int] | None = None) -> list[str]:
    """事件级先忆（检索是本地操作，不花 LLM 调用）。失败静默返回空。

    `exclude_src`：旧版排除本章 (vol, ch) 的碎片（H12，2026-09-05）——防止上一
    事件摘要进【相关前情】诱导复述。**2026-09-06 用户拍板放开**（ADR-013 完全体）：
    所有信息记忆实时更新，事件 i 必须能先忆到事件 i-1；复述防线改由
    strip_seam_overlap（拼接去重）+ seam_review（复述检测重写）+【相关前情】的
    「禁止复述」纪律共同承担。调用方不再传本章 exclude；参数保留供特殊场景。
    """
    try:
        from .memory import MemoryIndex, MemoryQuery, MemoryRetriever

        idx = MemoryIndex.load(ws, project_id)
        if not idx.fragments:
            idx.rebuild(ws, project_id, embedding)
        if not idx.fragments:
            return []
        hits = MemoryRetriever(idx, embedding=embedding).query(
            MemoryQuery(query=query, top_k=top_k))
        out = []
        for h in hits:
            if h.score <= 0:
                continue
            if exclude_src is not None \
                    and (h.source or {}).get("vol") == exclude_src[0] \
                    and (h.source or {}).get("ch") == exclude_src[1]:
                continue
            out.append(f"- [{h.kind} @ {h.source.get('vol')}:{h.source.get('ch')}] {h.text}")
        return out
    except Exception:  # noqa: BLE001
        return []


def _live_state_block(ws, project_id: str, cast_names: list[str],
                      bible_chars: list[dict] | None) -> str:
    """事件级实然状态块（ADR-013 完全体·2026-09-06 用户拍板）。

    worldstate 的事件级回写（apply_delta/_apply_time 逐事件落盘）早已存在，
    但读取侧从未接入事件 prompt——fame5 实测修为穿帮（角色卡「炼气九层」与
    细纲「炼气三层」两个静态计划态来源打架，各事件各选一边）。本 helper 在
    每个事件 prompt 构建时**实时读盘**，输出本场 cast 的当前状态行，钉死层
    注入（prompt_budget.PINNED）。worldstate 无记录的角色跳过（首个事件后
    才建立状态行）；全场无记录 → 空串（不注入，行为与旧版一致）。
    """
    try:
        from . import worldstate as _wsmod

        name2id = {}
        for c in bible_chars or []:
            if isinstance(c, dict) and c.get("name") and c.get("id"):
                name2id[str(c["name"])] = str(c["id"])
        ids = [name2id[n] for n in cast_names or [] if n in name2id]
        if not ids:
            return ""
        lines = _wsmod.snapshot_lines(_wsmod.load(ws, project_id), char_ids=ids)
        if not lines:
            return ""
        return "\n".join([
            "【实然状态（截至上一事件，事件级回写实时维护）】以下为这些人物"
            "此刻的硬状态（修为/位置/持物/伤势）。正文**必须**与其一致："
            "不得让已死亡者行动、不得倒退修为、不得凭空改变所在地；"
            "角色卡与细纲中的状态若与此冲突，**以本块为准**（实然优先于计划态）：",
            *lines,
        ])
    except Exception:  # noqa: BLE001 - 实然块失败不影响生成
        return ""


def _recent_chapter_memory(ws, project_id: str, vol: int, ch: int, max_items: int = 3) -> list[str]:
    """最近 1 章固定回退（讨论第 8 轮·用户拍板）：相关检索召回的是语义相似事件，
    未必是时间上最近的事件——长卷下模型容易忘了刚发生的事。直接带最近一章事件。
    跨卷：卷首章回看前一卷末章（F3 修复，2026-09-05）。
    """
    try:
        import json as _json

        p = ws._abs(f"{project_id}/memory/plot_events.json")
        if not p.exists():
            return []
        events = _json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    loc = _prev_chapter_loc(ws, project_id, vol, ch)
    if loc is None:
        return []

    def _k(e: dict) -> tuple[int, int]:
        at = e.get("at") or {}
        try:
            return int(at.get("vol", 0) or 0), int(at.get("ch", 0) or 0)
        except (TypeError, ValueError):
            return 0, 0

    prev = [e for e in events if isinstance(e, dict) and (0, 0) < _k(e) <= loc]
    if not prev:
        return []
    latest = max(_k(e) for e in prev)
    return [f"- [最近 {latest[0]}:{latest[1]}] {e.get('summary', '')}"
            for e in prev if _k(e) == latest][:max_items]


# ---------------------------------------------------------------- 递归分层 B：世界观滚动补充

def _known_setting_terms(ws, project_id: str) -> set[str]:
    """已知设定名词集合（人物名/别名/地名/物品/功法/设定条目关键词/注册表规范名）。

    items/skills 曾缺席——物品名不在核对范围是"强化符追认"闭环的洞（prompt 作用审计
    §2.6/P0-B：自造词绕过名册直接洗白成设定）。
    """
    terms: set[str] = set()
    try:
        import json as _json

        def _load(rel: str):
            p = ws._abs(f"{project_id}/bible/{rel}")
            return _json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

        for c in (_load("characters.json") or []):
            if isinstance(c, dict):
                if c.get("name"):
                    terms.add(str(c["name"]))
                for a in (c.get("aliases") or []):
                    terms.add(str(a))
        for loc in (_load("locations.json") or []):
            if isinstance(loc, dict) and loc.get("name"):
                terms.add(str(loc["name"]))
        for rel in ("items.json", "skills.json"):
            for e in (_load(rel) or []):
                if isinstance(e, dict):
                    if e.get("name"):
                        terms.add(str(e["name"]))
                    for a in (e.get("aliases") or []):
                        terms.add(str(a))
        for s in (_load("settings.json") or []):
            if isinstance(s, dict):
                for kw in (s.get("keywords") or []):
                    terms.add(str(kw))
                if s.get("term"):
                    terms.add(str(s["term"]))
        from .registry import Registry

        for e in Registry.load(ws, project_id).all_entries():
            terms.add(e.name)
    except Exception:  # noqa: BLE001
        pass
    return terms


def _supplement_settings(ws, project_id: str, ev_text: str, provider) -> int:
    """世界观滚动补充（递归分层 B）——P0-B 闸门版：纯提案器，不自动入档。

    事件文本新专有名词 → LLM 提案 → **全部写 `bible/settings_pending.json` 待人工
    确认**。理由：命中名册的词本就是"已知"（提前跳过），能到这里的名册外新词一律
    不入 settings.json——反向追认通道关死（prompt 作用审计 P0-B："强化符"类自造词
    不能再洗白成设定）。人工确认走 `novelist settings-pending --allow/--deny`。

    返回待确认条数。LLM/解析失败静默 0。
    """
    if provider is None or not ev_text.strip():
        return 0
    try:
        import json as _json

        known = _known_setting_terms(ws, project_id)
        known_block = "、".join(sorted(known)[:120]) if known else "（无）"
        prompt = (
            f"你是设定编辑。下面是一段正文节选，其中可能提到**此前从未出现过的专有名词**"
            f"（地点/组织/功法/种族/规则/势力等需要读者理解的设定）。\n"
            f"已知设定（不要提取这些）：{known_block}\n\n"
            f"正文：{ev_text[-800:]}\n\n"
            f"输出 JSON 数组，每项：{{\"term\": \"新名词\", \"text\": \"一句话设定说明（30字内）\", "
            f"\"keywords\": [\"检索用关键词1\", \"关键词2\"]}}\n"
            f"只输出真正的新设定名词，最多 3 条，没有就输出 []。只输出 JSON。"
        )
        res = provider.complete(LLMRequest(
            messages=[LLMMessage(role="user", content=prompt)],
            max_tokens_out=400, temperature=0.3, response_format="json_object",
            thinking=True))  # 判断类：新设定抽取决策，开思考
        if res.blocked or not (res.content or "").strip():
            return 0
        data = _json.loads(res.content.strip())
        if not isinstance(data, list):
            return 0
        pending_path = ws._abs(f"{project_id}/bible/settings_pending.json")
        pending = _json.loads(pending_path.read_text(encoding="utf-8")) if pending_path.exists() else []
        pending = pending if isinstance(pending, list) else []
        pending_terms = {p.get("term") for p in pending if isinstance(p, dict)}
        n = 0
        for item in data:
            if not isinstance(item, dict) or not item.get("term"):
                continue
            term = str(item["term"]).strip()
            if not term or term in known or term in pending_terms:
                continue
            pending.append({
                "term": term,
                "text": str(item.get("text") or f"关于{term}的设定")[:80],
                "keywords": [str(k) for k in (item.get("keywords") or []) if k][:4],
                "reason": "未命中已登记名册（characters/locations/items/skills/registry），"
                          "待人工确认（P0-B 闸门）",
            })
            pending_terms.add(term)
            n += 1
        if n:
            pending_path.write_text(_json.dumps(pending, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
        return n
    except Exception:  # noqa: BLE001
        return 0


# ---------------------------------------------------------------- 递归分层 C：重场戏拍展开

_EXPANDED_TAG = re.compile(r"\s*\[expanded\]\s*$")


def is_expanded_event(ev_text: str) -> bool:
    """细纲事件文本带 `[expanded]` 后缀 → 重场戏，递归拆拍（递归分层 C）。"""
    return bool(_EXPANDED_TAG.search(ev_text or ""))


def _strip_expanded_tag(ev_text: str) -> str:
    return _EXPANDED_TAG.sub("", ev_text or "").strip()


def _generate_beats(provider, system_prompt: str, goal: str, ev_text: str,
                    memories: list[str], setting_lines: list[str], related: dict,
                    readback_text: str, generation_tokens: int,
                    max_continuations: int, direct_words_floor: int,
                    content_tokens: int | None = None) -> tuple[str, int] | None:
    """重场戏拍展开（递归分层 C）：事件 → ≤3 拍逐拍生成。

    拍级生成带上一拍全文 + 前情摘要（非截断接缝——同一场景的连续动作）。
    任何拍失败返回 None（调用方回退事件级文本）。返回 (拼好的正文, 拍数)。
    """
    try:
        plan_prompt = (
            f"{goal}\n\n【重场戏拆解】事件「{_strip_expanded_tag(ev_text)}」需要拆成 2–3 个"
            f"连续拍（beat）来写厚，每拍一句话（15–40 字），覆盖：前奏 → 交锋/推进 → 收束。\n"
            f"只输出拍清单，每行一个，格式「N. 拍内容」。不要输出其他内容。"
        )
        res = provider.complete(LLMRequest(
            messages=[LLMMessage(role="system", content=system_prompt or ""),
                      LLMMessage(role="user", content=plan_prompt)],
            max_tokens_out=200, temperature=0.4,
            thinking=True))  # 判断类：重场戏拍级规划，开思考
        if res.blocked or not (res.content or "").strip():
            return None
        beats = []
        for ln in res.content.splitlines():
            ln = ln.strip().lstrip("-•*").strip()
            m = re.match(r"^(\d+)[.、)．]\s*(.+)$", ln)
            beats.append((m.group(2) if m else ln).strip())
        beats = [b for b in beats if b and len(b) <= 60][:3]
        if len(beats) < 2:
            return None  # 拆解失败 → 回退事件级

        pieces: list[str] = []
        for bi, btext in enumerate(beats, 1):
            parts = [f"{goal}", "",
                     f"【重场戏·拍 {bi}/{len(beats)}】{btext}"]
            if readback_text and bi == 1:
                parts += ["", readback_text]
            if pieces:
                # 拍级上下文：上一拍**全文**（非截断接缝）——同一场景的连续动作
                parts += ["", "【上一拍全文】（自然续写，不重复）：",
                          "…" + pieces[-1][-1500:]]
            if memories:
                parts += ["", "【相关前情】：", *memories]
            if setting_lines and bi == 1:
                parts += ["", "【本事件首次出现的设定】（自然带出）：", *setting_lines]
            if related:
                for k, lines in related.items():
                    if lines:
                        parts += ["", f"【相关{k}】", *lines[:4]]
            parts += ["", "篇幅约 150–300 字。"
                          + ("这是最后一个拍，须把该事件完整收束。" if bi == len(beats)
                             else "写到本拍结束即停，不要提前写下一拍内容。")]
            piece = _generate_with_continuation(
                provider, system_prompt or "", "\n".join(parts),
                generation_tokens, max_continuations, content_tokens=content_tokens)
            if len(piece) < direct_words_floor:
                return None  # 单拍失败 → 回退事件级
            # 拍级拼接同样要去掉复述（拍级上下文是上一拍全文，复述风险更高）
            if pieces:
                piece = strip_seam_overlap(pieces[-1], piece)
            pieces.append(piece)
        return "\n\n".join(pieces), len(beats)  # 拍间空行（同 P0 段落边界修复）
    except Exception:  # noqa: BLE001
        return None


def _generate_microbeats(provider, system_prompt: str, goal: str, ev_text: str,
                         memories: list[str], setting_lines: list[str], related: dict,
                         readback_text: str, generation_tokens: int,
                         max_continuations: int, direct_words_floor: int,
                         content_tokens: int | None = None) -> tuple[str, int] | None:
    """事件微拍规划（dp-microbeat，开关 `orchestrator.microbeat`，默认关）。

    在"重场戏专属拆拍"（`_generate_beats`）之上，提供**面向每个事件**的 2–4 拍规划，
    拍节奏为 起(设局)→承(推进/交锋)→转(转折/揭示)→合(收束)，末拍可带【钩】——
    收束同时向下一事件/章留一句悬念钩子，缓解"事件孤岛"（每章事件各卷各的、串不起来）。

    任一拍失败返回 None → 调用方回退事件级/重场戏路径——配套了"开关默认不影响现有产出"。
    各调用（拍级规划+逐拍生成）皆思考型透传，符合 dp-thinking-policy（规划属判断类）。
    """
    try:
        base = _strip_expanded_tag(ev_text)
        plan_prompt = (
            f"{goal}\n\n【事件微拍规划】事件「{base}」需拆成 2–4 个连续拍(beat)写厚，"
            f"覆盖节奏：起(设局/铺垫)→承(推进/交锋)→转(转折/揭示)→合(收束)；"
            f"末拍可标【钩】——收束的同时向下一事件/下章留一句悬念钩子，避免事件孤岛。\n"
            f"每拍一行「N. [阶段] 拍内容」，阶段取 起/承/转/合/钩 之一（15–40 字）。"
            f"只输出拍清单，不要输出其他内容。"
        )
        res = provider.complete(LLMRequest(
            messages=[LLMMessage(role="system", content=system_prompt or ""),
                      LLMMessage(role="user", content=plan_prompt)],
            max_tokens_out=200, temperature=0.4,
            thinking=True))  # 判断类：事件微拍规划，开思考
        if res.blocked or not (res.content or "").strip():
            return None
        beats = []
        for ln in res.content.splitlines():
            ln = ln.strip().lstrip("-•*").strip()
            m = re.match(r"^(\d+)[.、)．]\s*(.*)$", ln)
            if not m:
                continue
            body = m.group(2).strip()
            m2 = re.match(r"^[\[【](起|承|转|合|钩)[\]】]\s*(.+)$", body)
            stage = m2.group(1) if m2 else "承"
            text = (m2.group(2) if m2 else body).strip()
            if text:
                beats.append((stage, text))
        beats = [(s, t) for s, t in beats if len(t) <= 60][:4]
        if len(beats) < 2:
            return None  # 拆解失败 → 回退事件级

        pieces: list[str] = []
        total = len(beats)
        for bi, (stage, btext) in enumerate(beats, 1):
            parts = [f"{goal}", "", f"【事件微拍·{stage} {bi}/{total}】{btext}"]
            if readback_text and bi == 1:
                parts += ["", readback_text]
            if pieces:
                # 拍级上下文：上一拍**全文**（非截断接缝）——同一场景的连续动作
                parts += ["", "【上一拍全文】（自然续写，不重复）：",
                          "…" + pieces[-1][-1500:]]
            if memories:
                parts += ["", "【相关前情】：", *memories]
            if setting_lines and bi == 1:
                parts += ["", "【本事件首次出现的设定】（自然带出）：", *setting_lines]
            if related:
                for k, lines in related.items():
                    if lines:
                        parts += ["", f"【相关{k}】", *lines[:4]]
            if bi == total:
                tail = ("收束该事件，并自然带出向下一事件/章的钩子或余韵（如其真自然则做，不硬加）。"
                        if stage in ("钩", "合") else
                        "这是最后一个拍，须把该事件完整收束。")
            else:
                tail = "写到本拍结束即停，不要提前写下一拍内容。"
            parts += ["", "篇幅约 150–300 字。" + tail]
            piece = _generate_with_continuation(
                provider, system_prompt or "", "\n".join(parts),
                generation_tokens, max_continuations, content_tokens=content_tokens)
            if len(piece) < direct_words_floor:
                return None  # 单拍失败 → 回退事件级
            if pieces:
                piece = strip_seam_overlap(pieces[-1], piece)
            pieces.append(piece)
        return "\n\n".join(pieces), len(beats)  # 拍间空行
    except Exception:  # noqa: BLE001
        return None


def _prev_chapter_loc(ws, project_id: str, vol: int, ch: int,
                      *, max_scan: int = 200) -> tuple[int, int] | None:
    """定位"上一章"的位置：同卷 ch-1；卷首回退到**前一卷末章**（跨卷接缝，F3 修复）。

    返回 (vol, ch) 或 None（全书第一章）。按文件存在性从高章号向下扫。
    """
    if ch > 1:
        return vol, ch - 1
    pv = vol - 1
    if pv < 1:
        return None
    for c in range(max_scan, 0, -1):
        try:
            if (ws.draft_path(project_id, pv, c).exists()
                    or ws.chapter_path(project_id, pv, c).exists()):
                return pv, c
        except Exception:  # noqa: BLE001
            continue
    return None


def _prior_chapter_text(ws, project_id: str, vol: int, ch: int, max_chars: int = 1200) -> str:
    """回读机制（第七批第 4 条·用户拍板）：取前 1 章正文**原文**尾部（非摘要）。

    记忆摘要会丢细节（"赵铁山被问话时手抖了一下"这类伏笔级细节），
    回读原文补上；与最近章记忆回退互补——原文给细节、摘要给跨章语义。
    跨卷：卷首章回退到前一卷末章（F3 修复，2026-09-05）。
    """
    try:
        loc = _prev_chapter_loc(ws, project_id, vol, ch)
        if loc is None:
            return ""
        prev_vol, prev_ch = loc
        for base in (ws.chapter_path, ws.draft_path):
            p = base(project_id, prev_vol, prev_ch)
            if not p.exists():
                continue
            text = p.read_text(encoding="utf-8").strip()
            if not text:
                return ""
            if len(text) > max_chars:
                text = "…（前章节选）\n" + text[-max_chars:]
            return f"【上一章正文】（回读原文，延续其细节与节奏；不要重复已写内容）：\n{text}"
    except Exception:  # noqa: BLE001
        return ""


def _event_goal(chapter_goal: str, ev_text: str, idx: int, total: int, prev_piece: str,
                seam_chars: int, memories: list[str], is_last: bool,
                setting_lines: list[str] | None = None,
                related: dict | None = None,
                readback_text: str = "",
                cast_lines: list[str] | None = None,
                hist_lines: list[str] | None = None,
                direction_lines: list[str] | None = None,
                extra_readback: list[str] | None = None,
                appeared_notes: list[str] | None = None,
                prose_window: str = "",
                char_budget: int = 0,
                first_seen_lines: list[str] | None = None,
                banned_names: list[str] | None = None,
                line_cards: list[str] | None = None,
                live_state: str = "",
                prev_state: str = "") -> str:
    """装配单个事件的生成目标（细纲要点 + 人物调度 + 接缝上下文 + 先忆 + 设定 + RAG）。

    `related`：知识层检索结果注入行（讨论第 8 轮 RAG）。
    `readback_text`：前章正文原文（回读机制），只在第一个事件注入。
    `cast_lines` / `hist_lines` / `direction_lines` / `extra_readback`（ADR-020）：
    出场人物卡（无条件注入）、角色视角近况、人物调度单、久未出场角色的原文回读。
    `prose_window`：已写正文的最近片段（M3t 滑动窗口，反事件重演），段落边界切片。
    `char_budget`：prompt 总字符预算（M3t 预算器；0=不限，行为与旧版完全一致）。
    `prev_state`：dp-seam 上一事件结束状态锚；非空时优先于原文接缝（见下方 seam 块），
    缓解"接缝延续"与"禁复述"的假性两难。
    超预算时按 prompt_budget.EVICT_PRIORITY 从低价值层整段淘汰，钉死层不动。
    """
    from .prompt_budget import apply_prompt_budget

    blocks: list[tuple[str, str]] = [("goal", chapter_goal),
                                     ("step", f"【本步骤】只撰写本章第 {idx}/{total} 个事件：【{ev_text}】")]
    # 延迟拟题（ADR-020 决策一）：事件级一律不输出标题，标题在整章拼完后统一拟
    blocks.append(("discipline", "【输出纪律】直接写正文，**不要写章节标题**、不要写「第X章」字样；"
                                "不要重复上文已经写过的内容。"))
    if appeared_notes:
        blocks.append(("appeared", "\n".join([
            "【本章已出场记录（防重复登场）】下列人物在前面事件已出场过；"
            "本场再写他们时，登场方式与互动动作必须与记录不同，"
            "严禁重复同样的出场套路（如已'窜出来吓人'就不得再窜出来）：",
            *appeared_notes[-12:]])))
    if direction_lines:
        blocks.append(("direction", "\n".join([
            "【本场人物表演指令】（逐人遵守，违反即人物崩坏；"
            "各人的语气与取舍必须彼此不同）：", *direction_lines])))
    if line_cards:
        # ADR-025：线索卡钉死层（priority -1）——命中的线索必须在本场**真实推进**
        # （剧情动起来），不是复述账本；交织点两条线要互相作用，不是各写各的。
        blocks.append(("lines", "\n".join([
            "【本事件线索卡】（本场必须让下列线索的剧情向前走一步）：", *line_cards])))
    if live_state:
        # ADR-013 完全体（2026-09-06 用户拍板）：worldstate 实然状态行，钉死层。
        # 事件级回写（写侧）与本块（读侧）合起来才是"事件落定→全文档实时更新"。
        blocks.append(("live_state", live_state))
    if cast_lines:
        blocks.append(("cast", "\n".join([
            "【本场出场人物】（严格按各自的人设写：性格、称谓、立场、"
            "修为、与其他人的关系都要对得上）：", *cast_lines])))
    if first_seen_lines:
        # 方案6.1（质量加固 2026-09-05）：首次出场从"设定行软提示"升级为钉死层硬约束
        blocks.append(("first_seen", "\n".join([
            "【首次出场人物·硬性要求】下列人物首次/再度出场，**必须**在本事件内"
            "通过行动或对白自然交代其身份、与主角的关系（不得省略、不得只报名字；"
            "也不要写成人物简介式）：", *first_seen_lines])))
    if banned_names:
        # 方案6.4：广播拒绝点名 → 正文禁令（防"广播拒了、正文照样写"——ch1 苏晚晴实证）
        blocks.append(("banned", "\n".join([
            "【点名禁令】下列人物未经选角确认（不在本场可及池），"
            "**禁止**在正文中点名或提及，也不得让其暗中出场：",
            "、".join(banned_names)])))
    if hist_lines:
        blocks.append(("hist_lines", "\n".join([
            "【人物近况】（他们带着这些经历进入本场，言行要与之一致）：",
            *hist_lines])))
    if readback_text and idx == 1:
        blocks.append(("readback", readback_text))
    if extra_readback:
        blocks.append(("extra_readback", "\n".join(extra_readback)))
    if prose_window:
        blocks.append(("prose_window", "\n".join([
            "【前文正文（最近片段）】下面是已写正文的最近部分；续写必须自然衔接，"
            "片段里已经发生过的事件、已用过的出场方式与对白严禁再写一遍；"
            "片段中已出场的人物若再度登场，方式必须不同：",
            prose_window])))
    if prev_state:
        # dp-seam：用"上一事件结束状态锚"（事实）承接，而非紧贴原文末尾（措辞）——
        # 状态锚给自然衔接所需的事实，却不诱导模型复述上一事件的场景，解除"禁复述"两难。
        blocks.append(("state_seam", "\n".join([
            "【上一事件结束时的状态】以下为本事件开始前已发生的既成事实；"
            "本事件须从这里自然衔接，**不要重新描写上一事件的经过或场景**：",
            prev_state])))
    elif prev_piece:
        blocks.append(("seam", "\n".join([
            "【上文接缝】（从下面这段的结尾自然续写，不要重复已有内容）：",
            "…" + prev_piece[-seam_chars:]])))
    if memories:
        blocks.append(("memories", "\n".join([
            "【相关前情】（先忆，保持一致）：", *memories,
            "", "（注意：以上前情已写入正文，只作事实依据，"
                "**禁止复述其情节与场景**，直接推进新内容。）"])))
    if setting_lines:
        blocks.append(("settings", "\n".join([
            "【本事件首次出现的设定】（以下设定此前未在正文交代过，"
            "必须在本次事件里自然带出，让读者第一次见到就明白：）", *setting_lines])))
    if related:
        char_lines = related.get("character") or []
        if char_lines:
            blocks.append(("related:character", "\n".join([
                "【本事件相关人物】（按其性格/弧线/当前状态写）：", *char_lines])))
        set_lines = related.get("setting") or []
        if set_lines:
            blocks.append(("related:setting", "\n".join([
                "【相关知识·设定】（与本次事件相关的世界设定，须一致）：", *set_lines])))
        for key, label in (("thread", "伏笔"), ("lesson", "教训"), ("faction", "势力")):
            ls = related.get(key) or []
            if ls:
                blocks.append((f"related:{key}", "\n".join([
                    f"【相关知识·{label}】（本事件相关的{label}，保持连续）：", *ls])))
    blocks.append(("tail", "篇幅约 300–600 字。" + (
        "这是本章最后一个事件，结尾必须是一个完整的收束句。"
        if is_last else
        "不要写本章其他事件的内容，写到本事件结束即停。")))
    if char_budget > 0:
        blocks, _evicted = apply_prompt_budget(blocks, char_budget)
        # 淘汰可审计（M3t 纪律：绝不静默丢上下文）——先打印，后续接 ProductionResult
        if _evicted:
            print(f"[budget] evicted: {','.join(_evicted)}", flush=True)
    return "\n\n".join(text for _, text in blocks)


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


def _load_entity_tracker(ws, project_id: str):
    """加载统一实体追踪层（第八批）；失败返回 None（不启用，向后兼容）。"""
    try:
        from .entity import EntityTracker

        return EntityTracker.load(ws, project_id)
    except Exception:  # noqa: BLE001
        return None


def _lines_load_safe(ws, project_id: str) -> list[dict]:
    """线索账本安全读取（ADR-025）：任何异常返回 []，调用方按空账本降级。"""
    try:
        from .lines import load_lines

        return load_lines(ws, project_id)
    except Exception:  # noqa: BLE001
        return []


def _make_knowledge(ws, project_id: str, embedding):
    """构造知识检索层（讨论第 8 轮 RAG）；失败返回 None（生成照常，只少知识注入）。"""
    try:
        from .knowledge import KnowledgeBase

        return KnowledgeBase(ws, project_id, embedding=embedding)
    except Exception:  # noqa: BLE001
        return None


def _make_reviewer(ws, project_id: str, provider):
    """构造审校师（LLM 语义检）；失败返回 None（不阻断生成）。"""
    try:
        from ..consistency.reviewer import Reviewer

        return Reviewer(ws, project_id, llm=provider)
    except Exception:  # noqa: BLE001
        return None


def _style_tone(ws, project_id: str) -> str | None:
    """读 style.json 的 tone（语言风格 skill，第七批第 2 条）；无则 None。"""
    try:
        import json

        p = ws._abs(f"{project_id}/bible/style.json")
        if not p.exists():
            return None
        st = json.loads(p.read_text(encoding="utf-8"))
        tone = st.get("tone") if isinstance(st, dict) else None
        return tone if isinstance(tone, str) and tone else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- 递归分层 A：人物 JIT 补卡

def parse_cast_decl(gist_text: str) -> list[str]:
    """解析细纲 front-matter 的出场人物声明（递归分层 A 的确定性触发信号）。

    格式：`出场人物: [名字, 名字]` 或 `cast: [名字, 名字]`。
    细纲声明了谁出场，生成前就补谁的卡——出场即建档，防造人红线。
    """
    m = re.search(r"^(?:出场人物|cast):\s*\[(.*)\]\s*$", gist_text or "", re.M)
    if not m:
        return []
    out = []
    for piece in m.group(1).split(","):
        piece = piece.strip().strip("'\"").strip()
        if piece:
            out.append(piece)
    return out


def _jit_characters(ws, project_id: str, vol: int, ch: int, gist_text: str, provider):
    """人物缺卡告警（ADR-022 拍板后 JIT 补卡**降级为告警器**）。

    细纲声明出场但 bible 缺卡 → 不再 LLM 补卡入库（字面追认通道关死，与 P0-B 合围），
    改为：告警 + 缺名转角色工厂需求队列（source=jit_alarm），由工厂 `drain_queue`
    按配额生产注册。provider 参数保留兼容旧签名（已不使用）。
    返回入队需求数（0 = 无缺卡）。
    """
    names = parse_cast_decl(gist_text)
    if not names:
        return 0
    try:
        import json as _json

        chars = _json.loads(ws._abs(f"{project_id}/bible/characters.json").read_text(
            encoding="utf-8")) if ws._abs(f"{project_id}/bible/characters.json").exists() else []
        existing = set()
        for c in chars if isinstance(chars, list) else []:
            if isinstance(c, dict) and c.get("name"):
                existing.add(str(c["name"]))
                for a in (c.get("aliases") or []):
                    existing.add(str(a))
        missing = [n for n in names if n not in existing]
        if not missing:
            return 0
        from .character_factory import CharacterNeed, load_queue, queue_need

        # 幂等：同名缺卡需求已在队列（上章未消化）→ 不重复入队
        tag = "细纲声明出场但 bible 缺卡："
        queued_names = {q.description[len(tag):] for q in load_queue(ws, project_id)
                        if q.source == "jit_alarm" and q.description.startswith(tag)}
        missing = [n for n in missing if n not in queued_names]
        if not missing:
            return 0
        for n in missing:
            queue_need(ws, project_id, CharacterNeed(
                role="细纲声明出场（职能未指明）", description=f"{tag}{n}",
                source="jit_alarm", vol=vol, ch=ch))
        return len(missing)
    except Exception:  # noqa: BLE001 - 告警失败不阻断生成
        return 0


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


class _UsageCounter:
    """F7.1：包装 provider 聚合一次 produce_chapter 的全部 LLM 用量。

    produce_chapter 的 LLM 调用分散在嵌套函数（生成/续写/拍展开/JIT 补卡/编纂），
    逐个收集易漏；入口把 provider 换成计数器，整章（含嵌套与编纂）的
    `complete` 都经它过账，`__getattr__` 透明转发其余属性。
    """

    def __init__(self, provider):
        object.__setattr__(self, "_inner", provider)
        self.calls = 0
        self.tokens_in = 0
        self.tokens_out = 0

    def complete(self, req, **kwargs):
        self.calls += 1
        res = self._inner.complete(req, **kwargs)
        usage = getattr(res, "usage", None)
        if usage is not None:
            self.tokens_in += int(getattr(usage, "tokens_in", 0) or 0)
            self.tokens_out += int(getattr(usage, "tokens_out", 0) or 0)
        return res

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_inner"), name)


def _write_generation_audit(ws, project_id: str, vol: int, ch: int, counter: _UsageCounter,
                            *, ok: bool, mode: str = "tool", events: int = 0,
                            phase: str = "", note: str = "", jit: int = 0, settings: int = 0,
                            settings_pending: int = 0, world_now: int | None = None,
                            rel_pairs: int = 0, rel_proposals: int = 0) -> None:
    """F7.1：正文生成审计双落——`reports/stats/generation-<ts>.md`（人读持久）+ `.index.db`
    `audit_log`（机器查，ADR-016 辅助索引可重建）。先落报告文件、后同步索引（ADR-016
    写操作先文件后索引）；审计失败静默——审计不该阻断写章。

    计价与 forge 报告同口径（¥1/1M in + ¥2/1M out，DeepSeek 参考价）。
    `world_now`：本章结算后的故事时间（天数轴，C1/D 收敛落审计用）。
    `rel_pairs/rel_proposals`：ADR-023 章末实然关系账本（pair 数 / 翻转提案数）。
    """
    import json as _json
    import time as _time

    ts = _time.strftime("%Y%m%d-%H%M%S")
    try:
        rel = f"reports/stats/generation-{ts}.md"
        p = ws._abs(f"{project_id}/{rel}")  # noqa: SLF001
        p.parent.mkdir(parents=True, exist_ok=True)
        status = "ok" if ok else "failed"
        cost = counter.tokens_in / 1e6 * 1.0 + counter.tokens_out / 1e6 * 2.0
        lines = [
            f"# 正文生成报告 第 {vol} 卷第 {ch} 章（{ts}）",
            f"- 结果：{status}（mode={mode}）" + (f"，{note}" if note else ""),
            f"- 事件回写：{events} 条；JIT 补卡 {jit}；设定补充 {settings}；阶段 {phase or '-'}" + (f"；设定待确认 {settings_pending}" if settings_pending else ""),
            f"- LLM 调用 {counter.calls} 次：in={counter.tokens_in} / out={counter.tokens_out} tokens，"
            f"估算成本 ¥{cost:.4f}",
        ]
        if world_now is not None:
            lines.insert(1, f"- 故事时间：第 {world_now} 天（开书日=0）")
        if rel_pairs:
            lines.append(f"- 实然关系账本：{rel_pairs} 对；翻转提案 {rel_proposals} 条（enrich pending 待人工 allow）")
        ws.write_text(p, "\n".join(lines) + "\n")
        from ..storage.indexdb import IndexDb

        db = IndexDb(str(ws.index_db_path(project_id)))
        db.init()
        payload = {
            "ok": ok, "mode": mode, "events": events, "calls": counter.calls,
            "tokens_in": counter.tokens_in, "tokens_out": counter.tokens_out,
            "cost": round(cost, 6), "phase": phase, "jit": jit,
            "settings": settings, "settings_pending": settings_pending,
            "note": note, "ts": ts,
        }
        if world_now is not None:
            payload["world_now"] = world_now
        if rel_pairs:
            payload["rel_pairs"] = rel_pairs
            payload["rel_proposals"] = rel_proposals
        db.write_audit("produce_chapter", f"{vol}-{ch}", _json.dumps(payload, ensure_ascii=False))
    except Exception:  # noqa: BLE001 - 审计失败不影响写章
        pass


_TITLE_RE = re.compile(r"^#{1,6}\s*第\s*[一二三四五六七八九十\d]+\s*章")
# 行内/缩进变体：模型常把标题写在段落中间、或前面带引号/空格（实测 ch1 三个标题
# 均为「## 第 1 章 …」夹在正文里，行首 match 全部漏判）。search 版本用于全文扫描。
# 标题文字限定为不含标点/空白的紧凑串：`## 第 1 章 脚步声　叶岚推开门` 只抠
# 「## 第 1 章 脚步声」，同行正文「叶岚推开门」保留（否则 40 字符贪婪会吃掉正文）。
_TITLE_INLINE_RE = re.compile(
    r"#{1,6}\s*第\s*[一二三四五六七八九十百\d]+\s*章[^，。！？；：、\s\u3000]{0,30}")


def _dedupe_chapter_titles(text: str) -> str:
    """事件循环拼接后处理：模型常在每个事件开头重写章节标题，删除非首个标题。

    实测（诡夜大学 ch1）：3 事件拼出 4065 字，第 2 个事件开头又写了一遍
    「## 第 1 章 脚步声在午夜响起」——读者视角是标题重复。

    **前两章归因 P0 修订（2026-09-02）**：原实现用 `match`（只认行首）+ 整行丢弃，
    实测 3 个标题一个都没删掉：
    1. 改 `search`：行内标题（前面有空格/引号/正文）也能命中；
    2. 不再整行丢弃——标题后面常跟正文首句（"## 第 1 章 X　叶岚睁开眼"），
       只**抠掉标题片段**，保留同行的其余文字，避免吃掉正文；
    3. 抠掉后若该行只剩空白则整行删除。
    """
    kept: list[str] = []
    title_seen = False
    for ln in text.splitlines():
        m = _TITLE_INLINE_RE.search(ln)
        if m:
            if title_seen:
                rest = (ln[: m.start()] + ln[m.end():]).strip()
                if not rest:
                    continue
                kept.append(rest)
                continue
            title_seen = True
        kept.append(ln)
    return "\n".join(kept).strip("\n")


TITLE_PROMPT = """你是网文编辑。下面是刚写完的一章正文，请为它拟一个章节标题。

【要求】
1. 8–20 字，中文，不要写成一句完整的话
2. 点出这一章**最有戏的那一个点**（冲突、反转或揭示），不要概括全章流水账
3. 不要剧透后续章节的内容，不要写"终将""注定"这类升华腔
4. 不出现书名号、引号、括号、序号

只输出标题本身，不要任何说明、不要加「第X章」前缀。

正文开头：
"""


def _title_chapter(provider, text: str, vol: int, ch: int,
                   system_prompt: str | None = None) -> str:
    """延迟拟题（ADR-020 决策一）：整章成稿后按**成稿正文**拟标题。

    依据正文（而非细纲）拟题，标题才是结果而不是约束；失败返回空串（不阻断）。
    """
    if provider is None or not (text or "").strip():
        return ""
    head = text.strip()[:1200]
    tail = text.strip()[-600:] if len(text.strip()) > 1800 else ""
    sample = head + ("\n\n……\n\n" + tail if tail else "")
    try:
        res = provider.complete(
            LLMRequest(
                messages=[LLMMessage(role="system",
                                     content=system_prompt or "你是网文编辑。"),
                          LLMMessage(role="user", content=TITLE_PROMPT + sample)],
                max_tokens_out=64,
                temperature=0.3,
                thinking=False,  # 生成类：拟题，关思考
            )
        )
    except Exception:  # noqa: BLE001 - 拟题失败不阻断成稿
        return ""
    if res.blocked or not (res.content or "").strip():
        return ""
    raw = res.content.strip().splitlines()[0].strip()
    # 清洗：模型常自带 # 号、「第X章」前缀、引号或"标题："前缀
    raw = raw.lstrip("#").strip()
    raw = re.sub(r"^第\s*[一二三四五六七八九十百\d]+\s*章[^\w]*", "", raw).strip()
    raw = re.sub(r"^(标题|章节标题)\s*[:：]\s*", "", raw).strip()
    raw = raw.strip("《》「」『』\"'“”‘’ 。，、；：")
    raw = re.sub(r"\s+", "", raw)
    if not (4 <= len(raw) <= 30):
        return ""
    return raw


def _apply_chapter_title(ws, project_id: str, provider, final: str, vol: int, ch: int,
                         system_prompt: str | None = None) -> tuple[str, str]:
    """把拟得的标题写进正文首行，并回填细纲 front-matter `title`。

    返回 (正文, 标题)；拟题失败则原样返回。正文中若已有标题行（模型违规写的）
    先抠掉再统一加，避免两个标题。
    """
    title = _title_chapter(provider, final, vol, ch, system_prompt)
    if not title:
        return final, ""
    body = _dedupe_chapter_titles(final)
    lines = body.splitlines()
    if lines and _TITLE_INLINE_RE.search(lines[0]):
        body = "\n".join(lines[1:]).strip("\n")
    text = f"# 第 {ch} 章 {title}\n\n{body}"
    # 回填 outline front-matter（ADR-016 文件为主：细纲是给人看的，标题应在那里可见）
    try:
        p = ws.outline_chapter_path(project_id, vol, ch)
        if p.exists():
            md = p.read_text(encoding="utf-8")
            if md.startswith("---"):
                end = md.find("\n---", 3)
                if end > 0:
                    import json as _json

                    fm = md[3:end].strip()
                    try:
                        data = _json.loads(fm)
                    except ValueError:
                        data = None
                    if isinstance(data, dict) and data.get("title") != title:
                        data["title"] = title
                        new_md = ("---\n" + _json.dumps(data, ensure_ascii=False)
                                  + "\n" + md[end:])
                        ws.write_text(p, new_md)
    except Exception:  # noqa: BLE001 - 回填失败不影响成稿
        pass
    return text, title


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
    # ---- 第二批·人工审查：预算分离与篇幅治理 ----
    content_tokens: int | None = None,   # 正文预算（期望正文量）；None = 不额外约束
    # length_cap_chars 已按用户指示移除（2026-09-05）：上限从未 binding（实际 1900-2600
    # vs 8000），短章根因是切片生成+模型收束倾向，截上限只会破坏完整性不治短。
    max_events_per_chapter: int | None = None,  # 每章事件数上限；None = 不限制
    min_event_words: int | None = None,  # 单事件最小篇幅（字符，事件循环生效）；None = 沿用 direct_words_floor
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
    event_polish: bool = False,  # 每事件润色（第七批·用户拍板）；仅事件循环生效
    knowledge_llm: bool = True,  # RAG LLM 查询生成（讨论第 8 轮·用户设想）；False 退化为事件文本检索
    readback: bool = False,      # 回读机制（第七批第 4 条·用户拍板）：前 1 章正文原文注入
    jit_characters: bool = True,     # 递归分层 A：人物 JIT 补卡（第七批·用户拍板）
    supplement_settings: bool = False,  # 递归分层 B：世界观滚动补充（第七批·用户拍板）
    # ---- ADR-020 生成期人物一致性四件套 ----
    defer_title: bool = True,        # 决策一：延迟拟题（整章成稿后才按正文拟标题）
    cast_injection: bool = True,     # 决策二：出场人物卡无条件注入
    character_direction: bool = True,  # 决策三：人物调度层（细纲与正文之间的第 3 次细化）
    perspective_memory: bool = True,   # 决策四：角色视角记忆（事件末调用 + 无条件注入 + 双阈值回读）
    agentic_chronicle: bool = False,   # ADR-032 F1：编纂走只读证据仲裁环（慢性重冲突取证后落库）
    agentic_review: bool = False,      # ADR-032 F2：审校走只读证据环（可疑点取证后再定论）
    agentic_chronicle_rounds: int = 6,  # F1 取证预算（取证轮次上限，宁漏勿误杀，CLI 可配，ADR-032）
    agentic_review_rounds: int = 8,     # F2 取证预算上限（ADR-032）
    # ---- ADR-021 世界广播选角（一致性栈第 0 层：先定"谁该在场"，N3 调度才有意义）----
    broadcast_casting: bool = True,    # 事件级选角 LLM 推理（默认开；B1 验收已过，flash+max_tokens
                                       # 16000 批跑 18 事件 degrade=0/miss=0/增益 21 全合理，
                                       # 见 docs/问题总账 B1 与 ADR-021）
    # ---- 批次三（2026-09-05 用户拍板 1/2/3/4 全做）----
    seam_review: bool = False,     # 方案2：事件接缝 LLM 复述审查（strip_seam_overlap 的语义层）
    volume_facts: bool = False,    # 方案3：卷末章生成后自动产"本卷事实清单"（LLM 通读本卷）
    reset_state: bool = True,      # 写章前按 (vol,ch) 清理上一轮回写（重跑幂等，F4/I1-I4）
    microbeat: bool = False,       # 事件微拍规划（dp-microbeat）：每事件 reasoner 先产
                                   # 2–4 拍(起承转合/钩子)再逐拍生成；默认关→完全保持现有产出
    seam_state: bool = False,      # 状态锚接缝（dp-seam）：事件接缝从"原文末尾"改
                                   # "上一事件结束状态锚"（事实），缓解"续写与禁复述"两难；
                                   # 默认关→回退原文接缝，完全保持现有产出
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
    from .polish import completeness, polish_chapter, strip_near_dup_sentences

    sess = session or SessionInfo(project_id=project_id, agent="orchestrator")
    # F7.1：整章 LLM 用量聚合（含嵌套函数与编纂），出口统一写审计
    provider = _UsageCounter(provider)

    # ---- 0) 章节重写前置清理（2026-09-05 F4/I1-I4：重跑不再新旧并存）----
    # 先快照受影响状态文件再按 (vol,ch) 回退记忆层+时间轴；生成失败则 restore，
    # 避免"清了旧数据又没写进新数据"的中间态。母题/实体/tick 的按章幂等内置于各自模块。
    _reset_snap = None
    if reset_state:
        try:
            from .chapter_reset import ChapterSnapshot, prepare_rewrite, snapshot_report

            _reset_snap = ChapterSnapshot(ws, project_id)
            _reset_snap.capture()
            _reset_stats = prepare_rewrite(ws, project_id, vol, ch, embedding=embedding)
            _removed = (sum(int(v) for v in (_reset_stats.get("memory") or {}).values())
                        + int((_reset_stats.get("timeline") or {}).get("removed") or 0))
            if _removed:
                print(f"[ch {vol}-{ch}] {snapshot_report(_reset_stats)}", flush=True)
        except Exception:  # noqa: BLE001 - 前置清理失败不阻断写章（回退旧语义）
            _reset_snap = None

    # ---- 1) 圣经注入（B-02）----
    bible_injected = False
    # 声明式事件清单（第二批第 2 条）：key_events 写在细纲 front-matter，生成期直接迭代
    # （提前读取——JIT 补卡与事件循环都要用细纲文本）
    gist_text_for_events = ""
    try:
        _gist_p = ws.outline_chapter_path(project_id, vol, ch)
        if _gist_p.exists():
            gist_text_for_events = _gist_p.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        gist_text_for_events = ""

    # 递归分层 A（第七批第 5 条·用户拍板；ADR-022 后降级为告警器）：细纲声明出场但 bible
    # 缺卡 → 不再补卡，告警 + 缺名转角色工厂需求队列（追认通道关死，与 P0-B 合围）。
    jit_added = 0
    settings_pending = 0  # P0-B：未命中名册、进 settings_pending.json 待人工确认数
    if jit_characters:
        jit_added = _jit_characters(ws, project_id, vol, ch, gist_text_for_events, provider)
    # 角色工厂（ADR-022，M3q）：章前消化需求队列（JIT 告警/广播/CLI 转介），
    # 按每章配额生产注册；失败静默不阻断生成。
    factory_added = 0
    if provider is not None:
        try:
            from .character_factory import drain_queue as _factory_drain

            for _r in _factory_drain(ws, project_id, provider, vol=vol, ch=ch):
                if not _r.ok:
                    continue
                if _r.reused:   # 检索复用（未造新卡）：登记回链，不进 factory_added 计数
                    print(f"[ch{ch}] 需求复用现有角色：{_r.reused}"
                          f"（{_r.rejections[-1] if _r.rejections else ''}）", flush=True)
                else:
                    factory_added += 1
        except Exception:  # noqa: BLE001
            factory_added = 0
    if system_prompt is None and final_goal is None and inject_bible:
        try:
            # 历史教训不进 system prompt（第 8 轮 RAG 化后死参数已删，审计 §2.1）——
            # review_lessons.json 只走知识层检索路径
            ctx = build_chapter_context(ws, project_id, vol, ch, memories=memories,
                                        genre=genre, event_loop=event_loop)
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

    # ---- 1.5) 阶段判定（第九批：开篇/行文/收尾，确定性规则不调 LLM）----
    # 判据：tail=卷剩余章数≤K（硬时间约束，优先）；opening=卷内章号≤N 或 established 占比低。
    # 差异四维度：实体配额 / 篇幅系数 / 设定分批 / 校验强度——见 core/phase.py。
    from .phase import (Phase, PhasePolicy, VolumeContext, established_ratio,
                        payoff_checklist, payoff_prompt_lines)
    phase = Phase.WRITING
    phase_reason = ""
    phase_policy = None
    vctx = None
    entity_tracker = None
    payoff_lines: list[str] = []
    try:
        phase_policy = PhasePolicy.load(ws, project_id)
        vctx = VolumeContext.load(ws, project_id, vol)
        entity_tracker = _load_entity_tracker(ws, project_id)
        phase, phase_reason = phase_policy.judge(vctx, ch, established_ratio(entity_tracker))
        if phase is Phase.TAIL:
            checklist = payoff_checklist(ws, project_id, vol, ch, tracker=entity_tracker)
            payoff_lines = payoff_prompt_lines(
                checklist, is_final=bool(vctx.end and ch >= vctx.end))
    except Exception:  # noqa: BLE001 - 阶段判定失败退回行文期行为，不阻断写章
        phase, phase_reason, payoff_lines = Phase.WRITING, "", []
    if phase_policy is not None and phase is not Phase.WRITING:
        # 篇幅阶段系数：开篇该从容展开（×1.3），收尾按回收清单定
        generation_tokens = phase_policy.generation_tokens(generation_tokens, phase)
    if payoff_lines:
        goal = goal + "\n\n" + "\n".join(payoff_lines)

    # ---- 1.6) 临近事项分档注入（ADR-019 §3.3.3，M3m T2）----
    # 确定性规则零 LLM：>30% 静默、≤30% 轻提示、≤10% 或到期强提示。
    # 到期只升提示强度、不硬插剧情（与 ADR-002 大纲驱动一致）。
    try:
        from . import worldstate as _wsmod
        from .timeline import reminder_lines

        pending_lines = reminder_lines(_wsmod.load(ws, project_id))
    except Exception:  # noqa: BLE001 - 提醒注入失败不影响写章
        pending_lines = []
    if pending_lines:
        goal = goal + "\n\n" + "\n".join(pending_lines)

    # ---- 2) 生成（事件循环 / 剧本草稿 / 完整性校验与续写，B-04 + 第二、三批讨论）----
    mode = "tool"
    attempts = 0
    # ADR-020 计数器：拟题 / 调度单 / 角色视角（事件循环内累加，出口进 ProductionResult）
    chapter_title = ""
    directions_built = 0
    perspectives_written = 0
    appeared_notes: list[str] = []  # D12：本章已出场记录（跨切片位置记忆，防重复登场）
    broadcasts_built = 0  # ADR-021：本章广播成功次数（事件级选角推理）
    rel_pairs = 0         # ADR-023：章末实然关系账本 pair 数
    rel_proposals = 0     # ADR-023：阈值触发的翻转提案数（入 enrich pending）

    # ---- 2.1) 定时事项软 block（ADR-019 §3.3.3，M3m T2）----
    # 已到期且连续 block 的 pending：本章细纲/key_events 未体现 → 拦截。
    # 拦截后必须记账（tick），连续 3 次自动 expired 放行，防挂机批跑死锁。
    try:
        from .timeline import soft_block_check
        from .timeline import tick as _timeline_tick

        blocks = soft_block_check(ws, project_id, gist_text_for_events)
        if blocks:
            _timeline_tick(ws, project_id, vol=vol, ch=ch)
            _write_generation_audit(ws, project_id, vol, ch, provider, ok=False, mode=mode,
                                    phase=getattr(phase, "value", phase), note="soft-block 拦截")
            return ProductionResult(
                ok=False, result="\n".join(blocks), mode=mode,
                bible_injected=bible_injected,
                phase=getattr(phase, "value", phase), phase_reason=phase_reason)
    except Exception:  # noqa: BLE001 - 定时拦截失败不阻断写章
        pass
    comp: dict = {}
    chronic_reports: list = []
    review_blocks = 0       # 事件级审校 block 数（讨论第 7 轮）
    events_revised = 0      # 因 block 重写的事件数
    events_capped = 0       # 每章事件数超限截掉的事件数（第二批·人工审查）
    lessons_added = 0       # 本轮沉淀的历史教训数
    near_dup_removed = 0    # 章级近重复（事件重演/结尾段复制）删除的句数
    seam_hits = 0           # 接缝审查命中数（批次三·方案2）
    seam_repairs = 0        # 接缝复述重生成次数（≤ _SEAM_REPAIR_MAX）
    goal_bans: list[str] = []   # goal 注入的禁令（批次三·方案4 复用给润色）
    prev_facts_txt = ""         # 前卷事实清单（批次三·方案3/4 共用）
    soft_failures: list[str] = []  # H1：增强层失败留痕（与"检查通过"区分）
    try:
        if prefer_direct:
            mode = "direct"
            final = ""

            # ---- 方案 A/D（2026-09-05）+ 批次三：全局禁令与前卷事实注入 goal ----
            # 母题账本（D，行文机械闸）+ 细纲连读审查（A，语义闸）+ 蓝图连读（批次三·1）
            # → 追加进 goal（goal 块为 prompt 预算钉死层，事件/直出两条路径全覆盖）。
            _bans: list[str] = []
            try:
                from .motif import MotifLedger

                _bans += MotifLedger.load(ws, project_id).ban_lines()
            except Exception:  # noqa: BLE001
                pass
            try:
                from ..forge.coherence import load_coherence_bans, load_blueprint_bans

                _bans += load_coherence_bans(ws, project_id, vol, ch)
                _bans += load_blueprint_bans(ws, project_id)
            except Exception:  # noqa: BLE001
                pass
            if _bans:
                goal = goal + ("\n\n【重复禁令】下列动作/意象母题或问题已被全局审查"
                               "标记为重复，本章**禁止**再出现，必须换全新写法：\n"
                               + "\n".join(f"- {b}" for b in _bans[:10]))

            # ---- 批次三·方案3：前卷事实清单注入（跨卷状态/时间线/伏笔连续性）----
            _prev_facts = ""
            try:
                from .volume_facts import load_prev_facts

                _prev_facts = load_prev_facts(ws, project_id, vol)
            except Exception:  # noqa: BLE001
                pass
            if _prev_facts:
                goal = goal + ("\n\n【前卷事实清单】截至上一卷末的既成事实"
                               "（人物状态/时间线/未回收伏笔）。本章必须与之保持连续，"
                               "不得矛盾、不得让已完成的事再发生一遍：\n" + _prev_facts)
            goal_bans = _bans          # 方案4：润色复用同一份全局视野
            prev_facts_txt = _prev_facts

            if screenplay:
                # 剧本草稿（第三批第 2 条·档 2）：先剧本体写对白交锋，再叙事化。
                # 多声音质感 ↑，成本 ×2，无失控风险；重场戏专用。
                script_res = provider.complete(
                    LLMRequest(
                        messages=[LLMMessage(role="system", content=system_prompt or ""),
                                  LLMMessage(role="user", content=goal + _SCRIPT_INSTRUCTION)],
                        max_tokens_out=generation_tokens,
                        thinking=False,  # 生成类：剧本体草稿，关思考
                    )
                )
                script_text = (script_res.content or "").strip()
                if script_text:
                    goal = (goal + "\n\n【对白草稿】（仅作素材：保留其中对白原话与冲突走向，"
                            "转化为小说叙事，补足动作、场景与心理描写，不要保留剧本格式）\n"
                            + script_text)

            key_events = parse_key_events(gist_text_for_events) if event_loop else []
            # 每章事件数上限（第二批·人工审查）：思考开销按次付，事件切太细单位成本
            # 反而上升。超限只取前 N 个，多余事件记入 events_capped（告警可见）。
            events_capped = 0
            if key_events and max_events_per_chapter:
                events_capped = max(0, len(key_events) - max_events_per_chapter)
                key_events = key_events[:max_events_per_chapter]
            if key_events:
                # 事件循环（第二批第 2 条）：按声明式 key_events 逐事件推进、逐事件回写。
                # 这是 ADR-013「事件落定即回写」的落地——章内后续事件可先忆到上一事件。
                pieces: list[str] = []
                seam = max(200, min(400, _SEAM_CHARS))
                prev_end_state = ""  # dp-seam：上一事件的结束状态锚（非空时事件接缝用它）
                _prev_cast_names: list[str] = []  # ADR-021：上一事件出场者（广播"前情"输入）
                banned_names: list[str] = []  # 方案6.4：本章被广播拒绝的点名（正文禁令）
                settings_idx = (settings if settings is not None
                                else _load_settings(ws, project_id))
                # 知识检索层（讨论第 8 轮 RAG）：每章构建一次（语义向量化秒级），
                # 每事件按 LLM 生成的查询 + 事件文本检索相关知识注入
                knowledge = _make_knowledge(ws, project_id, embedding)
                readback_text = (_prior_chapter_text(ws, project_id, vol, ch)
                                 if readback else "")
                # ADR-020：人物层三件套的一次性准备（细纲出场声明每章解析一次，
                # 事件循环内只做匹配）。角色卡改**每事件重载**（ADR-013 完全体：
                # 事件级回写会更新角色卡 power，下一事件必须读到新卡——本地 JSON
                # 读取成本可忽略）。
                from . import director as _director
                _need_chars = any((cast_injection, character_direction, perspective_memory))
                declared_cast = parse_cast_decl(gist_text_for_events)
                # ADR-025：线索事件层注入的准备（每章一次）。账本缺失 = 空操作（降级）；
                # 章纲 lines_present 声明随细纲 md 行内 JSON 带过来（render_gist_md 写入）。
                from .lines import event_view as _lines_event_view
                from .lines import load_lines as _lines_load
                from .lines import parse_line_decl as _lines_parse_decl

                chapter_line_decls = _lines_parse_decl(gist_text_for_events)
                ledger_lines = _lines_load(ws, project_id)
                _lines_K = vctx.total_chapters() if vctx is not None else 0
                # 统一实体追踪（第八批：四阶段 + 别名消歧 + 松预算）
                entity_tracker = entity_tracker or _load_entity_tracker(ws, project_id)
                for idx, ev_text in enumerate(key_events, 1):
                    is_last = idx == len(key_events)
                    # ADR-013 完全体：角色卡每事件重载（上一事件的回写已更新 power）
                    bible_chars = (_director.load_characters(ws, project_id)
                                   if _need_chars else [])
                    # ADR-013 完全体：先忆不排除本章——事件 i
                    # 检索含事件 i-1 摘要（回写即增量更新，复述由 seam 层防线兜底）
                    memories_ev = _recall_for(ws, project_id, ev_text, embedding)
                    # 最近 1 章固定回退（讨论第 8 轮·用户拍板）
                    recent_ev = _recent_chapter_memory(ws, project_id, vol, ch)
                    if recent_ev:
                        memories_ev = recent_ev + memories_ev
                    # 设定按需注入：本事件命中且未交代的条目（首次交代状态机，讨论决策）
                    # 开篇期分批（第九批）：每事件最多注入 opening_settings_max 条
                    if settings_idx is not None:
                        setting_lines = settings_idx.pending_lines(goal, ev_text, ch=ch)
                        if phase is Phase.OPENING and phase_policy is not None:
                            setting_lines = settings_idx.pending_lines(
                                goal, ev_text, ch=ch,
                                max_entries=phase_policy.opening_settings_max)
                    else:
                        setting_lines = []
                    # 开篇期（第九批）：新名字配额纪律 + 上章推迟实体优先介绍
                    if phase is Phase.OPENING and phase_policy is not None \
                            and entity_tracker is not None:
                        deferred = entity_tracker.deferred_names()
                        if deferred:
                            setting_lines = list(setting_lines) + [
                                "- 上章被推迟的实体，本章优先安排出场/介绍："
                                + "、".join(deferred[:4])]
                        setting_lines = list(setting_lines) + [
                            f"- 本章新名字配额：至多 {phase_policy.opening_quota} 个；"
                            "细纲未声明的实体宁可不出现，留到后续章节"]
                    # 实体按阶段差异化注入（第八批）：stage 不足 described 的命中实体
                    # → 提示展开介绍；已 established 的名字行防重复介绍
                    if entity_tracker is not None:
                        expand = entity_tracker.needs_expansion(ev_text, ch=ch)
                        if expand:
                            setting_lines = list(setting_lines) + [
                                f"- 实体「{e.name}」首次/再度出场：通过行动/对白自然展开"
                                f"介绍（身份、与主角的关系），不要写成人物简介"
                                for e in expand[:3]]
                    # RAG：LLM 查询生成 → 融合检索 → 事件级注入行
                    related: dict = {}
                    if knowledge is not None:
                        queries = knowledge.plan_queries(
                            ev_text, provider if knowledge_llm else None)
                        items: list = []
                        for q in queries:
                            for it in knowledge.retrieve(q):
                                if it.id not in {x.id for x in items}:
                                    items.append(it)
                        related = {k: knowledge.lines(items, k, vol=vol, ch=ch)
                                   for k in ("setting", "character", "thread", "lesson", "faction")
                                   if knowledge.lines(items, k, vol=vol, ch=ch)}
                    # ---- ADR-020：本场人物层（cast → 近况 → 回读 → 调度单）----
                    cast_cards: list[dict] = []
                    cast_names: list[str] = []
                    cast_lines: list[str] = []
                    hist_lines: list[str] = []
                    direction_lines: list[str] = []
                    extra_rb: list[str] = []
                    if bible_chars:
                        # ADR-021 世界广播选角（一致性栈第 0 层，事件级"谁该在场"推理）：
                        # 语义推理 → 确定性校验（细纲声明不可删/池内/文本命中必含/≤6/不造名）
                        # → 名单驱动 match_cast；广播失败/关 → 静默降级走细纲声明+字面兜底
                        # 的确定性路径（绝不阻断生成，与 ADR-020 新增调用同一纪律）。
                        cast_source: list[str] = []
                        if broadcast_casting:
                            try:
                                from . import broadcast as _broadcast
                                _text_hits = [str(c.get("name") or "") for c in
                                              _director.cast_from_text(bible_chars, ev_text)]
                                _seam_txt = (pieces[-1][-300:] if pieces
                                             else (readback_text or "")[-300:])
                                _dec = _broadcast.broadcast_cast(
                                    ws, project_id, provider, vol=vol, ch=ch, idx=idx,
                                    ev_text=ev_text, seam=_seam_txt,
                                    declared=declared_cast, prev=_prev_cast_names,
                                    text_hits=_text_hits, system_prompt=system_prompt)
                            except Exception:  # noqa: BLE001 - 广播任何异常都降级
                                _dec = None
                            if _dec is not None and _dec.names:
                                cast_source = _dec.names
                                broadcasts_built += 1
                                if _dec.alarms:
                                    print(f"[ch{ch} e{idx}] 广播校验: "
                                          + "; ".join(_dec.alarms)[:300], flush=True)
                            if _dec is not None:
                                # 方案6.4：被拒点名收集 → 后续事件正文禁令（含名单为空仅拒绝的情形）
                                for _rn in (getattr(_dec, "rejected", None) or []):
                                    if _rn not in banned_names:
                                        banned_names.append(_rn)
                                if _dec.alarms and not _dec.names:
                                    print(f"[ch{ch} e{idx}] 广播校验: "
                                          + "; ".join(_dec.alarms)[:300], flush=True)
                        if cast_source:
                            # 广播名单（细纲声明与文本命中已由校验强制涵盖，勿再重复补）
                            cast_cards = _director.match_cast(bible_chars, cast_source)
                            cast_names = [str(c.get("name") or "") for c in cast_cards]
                        else:
                            # 降级路径（原确定性选角）：细纲声明为准，
                            # 再补事件文本里出现但声明漏掉的（≤2 人，防噪声）
                            cast_cards = _director.match_cast(bible_chars, declared_cast) \
                                if declared_cast else []
                            extra = [c for c in _director.cast_from_text(bible_chars, ev_text)
                                     if c["id"] not in {x["id"] for x in cast_cards}]
                            cast_cards = (cast_cards + extra[:2]) if cast_cards \
                                else _director.cast_from_text(bible_chars, ev_text)
                            cast_names = [str(c.get("name") or "") for c in cast_cards]
                        _prev_cast_names = list(cast_names)
                        if cast_injection:
                            # all_chars=bible_chars：关系目标 char:xxx 回查成角色名
                            cast_lines = _director.render_cards(cast_cards, bible_chars)
                            hist_lines = _director.render_history_lines(
                                ws, project_id, cast_cards)
                        # 回读（双阈值：事件计数 ≥3 或故事内 ≥30 天未见）
                        for c in cast_cards:
                            try:
                                need, _why = _director.needs_readback(
                                    ws, project_id, str(c.get("id") or ""))
                            except Exception:  # noqa: BLE001
                                need = False
                            if need:
                                ex = _director.readback_excerpt(ws, project_id, c)
                                if ex:
                                    extra_rb.append(ex)
                    # 人物调度层（细纲与正文之间的第 3 次细化）
                    sheet = None
                    if bible_chars and character_direction:
                        sheet = _director.build_direction(
                            ws, project_id, provider, vol=vol, ch=ch,
                            event_index=idx, ev_text=ev_text, cards=cast_cards,
                            history_lines=hist_lines, system_prompt=system_prompt)
                        if sheet is not None:
                            direction_lines = sheet.lines()
                            _director.save_direction(ws, project_id, sheet)
                            directions_built += 1
                    # M3t 正文滑动窗口：注入已写正文最近片段（含接缝），压制事件重演
                    # 与出场套路复用；切片按段落边界对齐（prompt_budget.prose_tail）
                    _prose_win = ""
                    if pieces:
                        from .prompt_budget import prose_tail
                        _prose_win = prose_tail("\n\n".join(pieces), _PROSE_WINDOW_CHARS)
                    # ADR-025：本场命中线卡（章纲声明优先 + 词元命中，≤3 条，交织自动标注）
                    _line_cards = (_lines_event_view(
                        ledger_lines, [d["id"] for d in chapter_line_decls],
                        ev_text, vol, ch, _lines_K)
                        if ledger_lines else [])
                    prompt = _event_goal(goal, ev_text, idx, len(key_events),
                                         pieces[-1] if pieces else "", seam,
                                         memories_ev, is_last, setting_lines, related,
                                         readback_text, cast_lines=cast_lines,
                                         hist_lines=hist_lines,
                                         direction_lines=direction_lines,
                                         extra_readback=extra_rb,
                                         appeared_notes=appeared_notes,
                                         prose_window=_prose_win,
                                         char_budget=_PROMPT_CHAR_BUDGET,
                                         line_cards=_line_cards,
                                         # ADR-013 完全体：实然状态块（实时读盘，
                                         # 钉死层注入——修为/位置/持物以 worldstate 为准）
                                         live_state=_live_state_block(
                                             ws, project_id, cast_names, bible_chars),
                                         # H9 修复（2026-09-05）：原先调用点漏传
                                         # banned_names——"广播拒绝点名"禁令整体是
                                         # 死代码。传入并与本场 cast 求差：若该人物
                                         # 已被本场选角确认（细纲声明不可删），禁令
                                         # 与人物卡同场矛盾，须剔除。
                                         banned_names=[n for n in banned_names
                                                       if n not in cast_names],
                                         prev_state=prev_end_state)
                    # D12 跨切片位置记忆：本场出场方式记入防重复记录
                    # （在 prompt 构建之后追加——本场记录只影响后续事件）
                    _sheet_map = {d.name: d.how for d in (sheet.characters if sheet else [])}
                    for _nm in cast_names:
                        _how = _sheet_map.get(_nm)
                        appeared_notes.append(
                            f"- {_nm}（第{idx}场）：{_how or '出场'}")

                    # 递归分层 B（第七批第 5 条·用户拍板；P0-B 闸门化）：世界观滚动补充——
                    # 事件文本新专有名词 → LLM 提案 → 全部进 settings_pending.json
                    # 待人工确认（追认通道关死），确认后经 settings-pending 入档。
                    if supplement_settings:
                        try:
                            settings_pending += _supplement_settings(ws, project_id, ev_text,
                                                                     provider)
                        except Exception:  # noqa: BLE001
                            pass

                    # 递归分层 C+（dp-microbeat，开关 orchestrator.microbeat，默认关）：
                    # 面向**每个事件**的 2–4 拍(起/承/转/合+钩)逐拍生成，缓解事件孤岛；
                    # 任一拍失败返回 None → 回退到下方重场戏拆拍 / 事件级路径（开关默认关
                    # → 完全保持现有产出）。
                    piece = None
                    if microbeat:
                        mb_res = _generate_microbeats(
                            provider, system_prompt or "", goal, ev_text,
                            memories_ev, setting_lines, related, readback_text,
                            generation_tokens, max_continuations, direct_words_floor,
                            content_tokens=content_tokens)
                        if mb_res is not None:
                            piece, _ = mb_res

                    # 递归分层 C（第七批第 5 条·用户拍板）：重场戏拍展开——
                    # 细纲事件标 [expanded] → 拆 ≤3 拍逐拍生成；任何拍失败回退事件级。
                    if piece is None and is_expanded_event(ev_text):
                        beat_res = _generate_beats(
                            provider, system_prompt or "", goal,
                            _strip_expanded_tag(ev_text),
                            memories_ev, setting_lines, related, readback_text,
                            generation_tokens, max_continuations, direct_words_floor,
                            content_tokens=content_tokens)
                        if beat_res is not None:
                            piece, _ = beat_res

                    if piece is None:
                        piece = _generate_with_continuation(
                            provider, system_prompt or "", prompt, generation_tokens,
                            max_continuations, content_tokens=content_tokens)
                    # 单事件最小篇幅（第二批·人工审查）：低于下限即失败重试，
                    # 避免"草草两句话一个事件"稀释正文密度
                    event_floor = max(direct_words_floor, min_event_words or 0)
                    if len(piece) < event_floor:
                        raise RuntimeError(
                            f"event {idx}/{len(key_events)} too short ({len(piece)} chars)")

                    # ---- 接缝审查（批次三·方案2）：LLM 挡"换措辞重演同一情节" ----
                    # strip_seam_overlap 只能删字面重叠；ch1"母亲来电"写两遍、
                    # 措辞完全不同——语义重演只能靠这里。命中 → 带 reject note 重生成一次。
                    if seam_review and pieces and seam_repairs < _SEAM_REPAIR_MAX:
                        _retell = _seam_retell(
                            provider, system_prompt or "",
                            pieces[-1][-_SEAM_WINDOW:], piece[:_SEAM_WINDOW])
                        if _retell:
                            seam_hits += 1
                            revised = _generate_with_continuation(
                                provider, system_prompt or "",
                                prompt + _SEAM_REJECT.format(what=_retell),
                                generation_tokens, max_continuations,
                                content_tokens=content_tokens)
                            if len(revised) >= event_floor:
                                piece = revised
                                seam_repairs += 1

                    # 每事件审校 + 修订（讨论第 7 轮）：block → 带建议重写 1 次。
                    # 审校只读产出工单，修订由编排层决定（docs/04 §5.4 双层门禁语义层）。
                    if event_review:
                        reviewer = _make_reviewer(ws, project_id, provider)
                        if reviewer is not None:
                            try:
                                # qwen3.6 真机教训：每事件后审校的是「单事件片段」，而
                                # REVIEW_PROMPT 默认按「完整一章」审——模型把单事件当整章，
                                # 拿整章细纲对照必然报「细纲未覆盖」（ch1/ch2 的 block 全是
                                # 这类误报），修订 prompt 又诱导模型提前补后续事件内容 →
                                # 事件边界污染/场景重演。传范围说明纠正审校预期。
                                _total = len(key_events) if key_events else 0
                                _scope = (
                                    f"【范围说明】本次审读的是本章第 {idx}/{_total} 个事件的"
                                    f"正文片段（{'已到章末' if is_last else '本章尚未写完'}）。"
                                    f"只审该片段内部的问题（设定矛盾/人设漂移/称谓/时间线/"
                                    f"战力越界/与前情冲突/片段内的细纲要点遗漏）；"
                                    f"本章后续事件的内容尚未出现，不构成「细纲未覆盖」。"
                                    if _total else "【范围说明】本次审读的是完整一章。")
                                issues = reviewer.review(
                                    piece, vol, ch, gist_text=gist_text_for_events,
                                    memories=memories_ev, scope=_scope)
                                if agentic_review:
                                    from ..consistency.reviewer_agent import agentic_review

                                    _ar = agentic_review(
                                        piece, vol, ch, ws=ws, project_id=project_id,
                                        provider=provider, session=sess, embedding=embedding,
                                        gist_text=gist_text_for_events, memories=memories_ev,
                                        scope=_scope, max_rounds=agentic_review_rounds)
                                    issues = _ar.issues
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
                                    generation_tokens, max_continuations,
                                    content_tokens=content_tokens)
                                if len(revised) >= direct_words_floor:
                                    piece = revised
                                    events_revised += 1
                                lessons_added += _append_lessons(ws, project_id, blocks, vol, ch)

                    # 事件级润色（人工审查第七批第 2 条·用户拍板）：写完即润色，
                    # 润色后的文本进接缝和记忆——后续事件继承润色风格，源头统一。
                    if event_polish:
                        from .polish import polish_chapter

                        try:
                            pr = polish_chapter(
                                piece, provider,
                                tone=_style_tone(ws, project_id),
                                max_tokens=min(1500, max(generation_tokens * 2, 800)),
                                is_chapter=False,
                                system_prompt=system_prompt)
                            if pr.changed:
                                piece = pr.text
                        except Exception:  # noqa: BLE001 - 润色失败保留原文
                            pass

                    # 事件拼接去重：模型常把接缝复述一遍（跨 v2→v5 老 bug）
                    if pieces:
                        piece = strip_seam_overlap(pieces[-1], piece)
                    pieces.append(piece)
                    attempts += 1
                    # 逐事件回写（ADR-013）；失败不阻断，交由章级兜底
                    if chronicler is None:
                        chronicler = _make_chronicler(ws, project_id, provider, embedding,
                                                      semantic_checker)
                    if chronicler is not None:
                        try:
                            if agentic_chronicle:
                                from .chronicler_agent import run_agentic

                                _rep, _arb = run_agentic(
                                    chronicler, piece, vol=vol, ch=ch, max_events=2,
                                    tag=f"e{idx}", payoff=(phase is Phase.TAIL),
                                    provider=provider, session=sess, embedding=embedding,
                                    max_rounds=agentic_chronicle_rounds,
                                )
                                chronic_reports.append(_rep)
                            else:
                                chronic_reports.append(
                                    chronicler.run(piece, vol, ch, max_events=2,
                                                   tag=f"e{idx}",
                                                   payoff=(phase is Phase.TAIL)))
                        except Exception:  # noqa: BLE001 - 单事件编纂失败不阻断整章
                            pass
                        # 角色视角记忆（ADR-020 决策四）：事件落定即记，一人一条视角
                        if perspective_memory and cast_names:
                            try:
                                perspectives_written += chronicler.record_perspectives(
                                    piece, cast_names, vol, ch, event_index=idx)
                            except Exception:  # noqa: BLE001 - 视角失败不阻断
                                pass
                    # dp-seam：为下一个事件产"结束状态锚"（非末事件；失败回退原文接缝）
                    if seam_state and not is_last:
                        _anchor = _event_end_state(provider, system_prompt or "",
                                                   ev_text, piece)
                        if _anchor:
                            prev_end_state = _anchor
                final = _dedupe_chapter_titles("\n\n".join(pieces))
                # 章级近重复确定性修复（ch7/ch8 实证）：事件循环里每个片段单独看过
                # 都没问题，拼起来才发现"同一收尾动作写了两遍"——片段级去重够不着，
                # 必须在拼接后对整章再扫一遍。句级删除，保留重演段里的新信息。
                if validate:
                    final, _near_removed = strip_near_dup_sentences(final)
                    if _near_removed:
                        near_dup_removed = _near_removed
                    # 章末问题清单反馈重试（direct 模式 :1564 起有同款闭环）：
                    # 拼接后的问题（截断/元叙事/残留重复）一次修复调用，问题减少才采纳。
                    if max_retries > 0:
                        try:
                            ev_problems = _completeness_problems(completeness(final))
                        except Exception:  # noqa: BLE001
                            ev_problems = []
                        # 方案6.2：首次出场身份线索并入审校清单（同走一次修复调用）
                        ev_problems = ev_problems + _first_appearance_problems(
                            entity_tracker, final)
                        if ev_problems:
                            repaired = _repair_chapter_text(
                                provider, system_prompt or "", final, ev_problems,
                                generation_tokens)
                            if repaired and len(repaired) >= direct_words_floor:
                                try:
                                    p2 = _completeness_problems(completeness(repaired))
                                except Exception:  # noqa: BLE001
                                    p2 = ev_problems
                                if len(p2) < len(ev_problems):
                                    final = repaired
            else:
                last_problems: list[str] = []
                for attempt in range(max_retries + 1):
                    attempts = attempt + 1
                    prompt = goal + (_REPAIR_HINT.format(problems="；".join(last_problems))
                                     if last_problems else "")
                    final = _generate_with_continuation(
                        provider, system_prompt or "", prompt, generation_tokens,
                        max_continuations, content_tokens=content_tokens)
                    if len(final) < direct_words_floor:
                        raise RuntimeError(f"direct generation too short ({len(final)} chars)")
                    comp = completeness(final)
                    problems = _completeness_problems(comp) if validate else []
                    # 方案6.2：direct 模式同样查首次出场身份线索
                    if validate:
                        problems = problems + _first_appearance_problems(
                            entity_tracker or _load_entity_tracker(ws, project_id),
                            final)
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
            # F2 修复（2026-09-05）：final 是 Agent 结束语（非正文），完整性判定
            # 不得基于它——直接跳过，落到下方"以草稿文件为准"分支。
            if validate and mode == "direct" and len(final) >= direct_words_floor:
                comp = completeness(final)
    except Exception as e:  # noqa: BLE001 - 循环/生成异常统一收敛为失败
        if _reset_snap is not None:
            try:
                _reset_snap.restore()  # 生成失败 → 还原前置清理，不留中间态
            except Exception:  # noqa: BLE001
                pass
        _write_generation_audit(ws, project_id, vol, ch, provider, ok=False, mode=mode,
                                phase=getattr(phase, "value", phase),
                                note=f"生成异常：{str(e)[:60]}")
        return ProductionResult(ok=False, result=str(e), mode=mode, bible_injected=bible_injected,
                                attempts=attempts, completeness=comp,
                                phase=getattr(phase, "value", phase), phase_reason=phase_reason,
                                events_capped=events_capped)

    if not comp and final:
        comp = completeness(final)
    if near_dup_removed:
        comp["near_dup_removed"] = near_dup_removed

    # ---- 3) 落盘草稿 ----
    draft = ws.draft_path(project_id, vol, ch)
    if mode == "direct":
        try:
            draft.parent.mkdir(parents=True, exist_ok=True)
            ws.write_text(draft, final + "\n")
        except Exception:  # noqa: BLE001
            if _reset_snap is not None:
                try:
                    _reset_snap.restore()
                except Exception:  # noqa: BLE001
                    pass
            _write_generation_audit(ws, project_id, vol, ch, provider, ok=False, mode=mode,
                                    phase=getattr(phase, "value", phase), note="草稿落盘失败")
            return ProductionResult(ok=False, result="cannot write draft", mode=mode,
                                    bible_injected=bible_injected, attempts=attempts, completeness=comp)

    if not comp:
        try:
            comp = completeness(draft.read_text(encoding="utf-8")) if draft.exists() else {}
        except OSError:  # pragma: no cover
            comp = {}

    # ---- 4) 文风润色（成章后额外一次 LLM 调用；tone 驱动，第七批第 2 条）----
    polish_res = None
    # F2 修复（2026-09-05）：tool 模式 final 是 Agent 结束语而非正文——
    # 润色与落盘一律以草稿文件内容为源，杜绝"结束语润色后覆盖整章"。
    polish_src = final
    if mode != "direct":
        try:
            if draft.exists():
                polish_src = draft.read_text(encoding="utf-8").strip() or final
        except OSError:
            polish_src = final
    # 事件模式此前**不做章级润色**（只做事件级，max_tokens 1500）——ch7/ch8 实证
    # AI 味 38.98→38.7 几乎不动：事件级润色各自为政，拼起来的整章从未被统一润过。
    # 章级润色按 `polish` 开关（默认关）对两种模式一视同仁。
    if polish and polish_src:
        # ---- 批次三·方案4：润色全局化——从"逐句美化"升级为"带全局的通读顺稿" ----
        # 实证：AI 味分润色前后基本不动（8.74→8.74），根因之一是润色只有本章文本、
        # 没有任何全局视野。这里注入：章位置/前章收尾/全局禁令/前卷事实摘要。
        _polish_global_parts: list[str] = []
        try:
            _polish_global_parts.append(f"本章位置：第 {vol} 卷第 {ch} 章")
            _prev_loc = _prev_chapter_loc(ws, project_id, vol, ch)  # F3：跨卷也取前章收尾
            if _prev_loc is not None:
                _prev_draft = ws.draft_path(project_id, *_prev_loc)
                if _prev_draft.exists():
                    _tail = _prev_draft.read_text(encoding="utf-8").strip()[-180:]
                    _polish_global_parts.append(f"前一章收尾：…{_tail}")
            if goal_bans:
                _polish_global_parts.append(
                    "全局审查标记的问题（润色时顺带自查，发现残留即删除）：\n" +
                    "\n".join(f"- {b}" for b in goal_bans[:6]))
            if prev_facts_txt:
                _polish_global_parts.append("前卷既成事实（人物状态/时间线必须连续）：\n"
                                            + prev_facts_txt[:500])
            # ADR-025：润色层线索禁令（<50 字；账本为空不加，行为与旧版一致）
            _plines = _lines_load_safe(ws, project_id)
            if _plines:
                _plines_txt = "线索禁令：暗线只可推进不可点破真相；已闭合线索不得再当活线写。"
                if len(_plines_txt) < 50:
                    _polish_global_parts.append(_plines_txt)
        except Exception:  # noqa: BLE001 - 全局块失败不影响润色本身
            pass
        _polish_global = ("\n\n【全书视野（通读顺稿用，禁止写进正文）】\n" +
                          "\n\n".join(_polish_global_parts)) if _polish_global_parts else ""
        try:
            polish_res = polish_chapter(polish_src, provider, vol=vol, ch=ch,
                                        tone=_style_tone(ws, project_id),
                                        is_chapter=True,
                                        system_prompt=system_prompt,
                                        global_context=_polish_global)
            if polish_res.changed:
                final = polish_res.text
                try:
                    ws.write_text(draft, final + "\n")
                except OSError:  # pragma: no cover
                    polish_res = None
        except Exception:  # noqa: BLE001 - 润色失败不阻断，保留原稿
            polish_res = None
            soft_failures.append("polish(润色失败，保留原稿)")

    # ---- 4.4) 篇幅硬上限已移除（用户 2026-09-05 拍板：取消一切字数相关需求）----
    # 实证：上限从未 binding（实际 1900-2600 字 vs 8000），截断只伤完整性。

    # ---- 4.4a) 首登场身份局部重写（方案 B）+ 母题记账（方案 D）----
    # B：检测 → 定位首现段落 → LLM 仅重写该段（身份织入动作/对白，不插简介段）。
    # D：成稿母题句入账本，供后续章生成时注入禁令。
    first_seen_patched = 0
    if final:
        try:
            trk = entity_tracker or _load_entity_tracker(ws, project_id)
            final, first_seen_patched = _patch_first_appearances(
                ws, project_id, vol, ch, final, provider, trk)
            if first_seen_patched and mode == "direct" and draft.exists():
                ws.write_text(draft, final + "\n")
        except Exception:  # noqa: BLE001 - 补写是增强层，失败不影响成稿
            first_seen_patched = 0
        try:
            from .motif import MotifLedger

            _led = MotifLedger.load(ws, project_id)
            _led.remove_chapter(ch)  # I2：重跑幂等——先清本章旧账再入新账
            _led.add_text(final, ch)
            _led.save(ws, project_id)
        except Exception:  # noqa: BLE001 - 账本失败不影响成稿
            pass

    # ---- 4.4b) 卷末事实清单（批次三·方案3，opt-in）：本卷最后一章生成后触发 ----
    # 依据蓝图判定"本卷最后一章"（蓝图无该卷后续章）；LLM 通读本卷正文产出
    # 人物状态/时间线/未回收伏笔/未决冲突，下一卷每章注入 goal。
    volume_facts_built = False
    if volume_facts and final:
        try:
            from ..forge.state import Blueprint

            _bp = Blueprint.load(ws, project_id)
            _later = []
            for _g in (_bp.section("chapters") or []):
                try:
                    if int(_g.get("vol") or 0) == vol and int(_g.get("ch") or 0) > ch:
                        _later.append(_g)
                except (TypeError, ValueError):
                    continue
            if not _later:
                from .volume_facts import build_volume_facts

                volume_facts_built = build_volume_facts(ws, project_id, provider, vol)
        except Exception:  # noqa: BLE001 - 事实清单是增强层，失败不影响成稿
            volume_facts_built = False

    # ---- 4.4c) 线索卷中检查点 / 卷末审计（ADR-025 批2，确定性零 LLM）----
    # 检查点：本卷约 50% 章落定后自查（active 线本卷零推进 → 写 due 强制处理项）；
    # 卷末审计：伏笔到期写 due（下卷卷纲强制项）+ 回收升级提名 + yield 缺失 +
    # 篇幅比告警，报告落 reports/lines-audit-vol{N}.md。账本空 = 全部跳过（降级）。
    if final:
        try:
            from ..forge.state import Blueprint as _l_bp_cls

            _l_bp = _l_bp_cls.load(ws, project_id)
            _l_K = int((((_l_bp.get("meta") or {}).get("scale")) or {}).get(
                "chapters_per_volume") or 0)
            _l_later = []
            for _g in (_l_bp.section("chapters") or []):
                try:
                    if int(_g.get("vol") or 0) == int(vol) and int(_g.get("ch") or 0) > ch:
                        _l_later.append(_g)
                except (TypeError, ValueError):
                    continue
            from .lines import (checkpoint as _l_chk, load_lines as _l_load,
                                volume_audit as _l_aud)
            _l_rows = _l_load(ws, project_id)
            if _l_rows:
                if not _l_later:
                    _l_audit = _l_aud(ws, project_id, vol, _l_K)
                    for _w in (_l_audit.get("warnings") or []):
                        soft_failures.append(f"线索卷末审计：{_w}")
                elif _l_K and ch == max(1, _l_K // 2):
                    for _w in _l_chk(ws, project_id, _l_rows, vol, ch, _l_K):
                        soft_failures.append(f"线索检查点：{_w}")
        except Exception:  # noqa: BLE001 - 检查点/审计失败不影响成稿
            pass

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

    # ---- 4.6) 实体进度更新（第八批：四阶段 + 别名消歧 + 松预算）----
    # 扫正文更新 stage/mentions/last_ch（别名共指消歧），超预算记告警。
    # 第九批：开篇期按配额判定，超出部分推迟（deferred 持久化，下章优先注入）。
    entity_alerts: list[str] = []
    entity_new = 0
    try:
        tracker = entity_tracker or _load_entity_tracker(ws, project_id)
        if tracker is not None:
            verify_text = draft.read_text(encoding="utf-8") if draft.exists() else final
            upd = tracker.update_from_chapter(verify_text, vol, ch)
            entity_new = len(upd.get("new") or [])
            if phase is Phase.OPENING and phase_policy is not None:
                entity_alerts = tracker.budget_check(
                    upd.get("new") or [], quota=phase_policy.opening_quota,
                    defer_over=True)
                tracker.clear_deferred(upd.get("new") or [])  # 已介绍的推迟项出队
            else:
                entity_alerts = tracker.budget_check(upd.get("new") or [])
            tracker.save()
    except Exception:  # noqa: BLE001 - 实体进度失败不影响成稿
        entity_alerts = []

    # ---- 4.7) 延迟拟题（ADR-020 决策一）----
    # 放在去重/润色/篇幅截断**全部之后**：此时的 final 才是最终正文，标题应描述它。
    # 放在设定交代验证与实体扫描之后，避免标题里的词被误判成新实体/新设定。
    if defer_title and mode == "direct" and final and not chapter_title:
        final, chapter_title = _apply_chapter_title(
            ws, project_id, provider, final, vol, ch, system_prompt)
        if chapter_title:
            try:
                ws.write_text(draft, final + "\n")
            except Exception:  # noqa: BLE001 - 落盘失败不影响返回成稿
                pass

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
                # H3 修复（2026-09-05）：inject_bible 是"圣经注入"开关（对照实验用），
                # 不得连坐章级事件回写——否则 --no-bible 实验组静默丢失全部回写。
                if chronicler is None:
                    from .chronicler import Chronicler

                    chronicler = Chronicler(ws, project_id, llm=provider, embedding=embedding,
                                            semantic_checker=semantic_checker)
                if chronicler is not None:
                    if agentic_chronicle:
                        from .chronicler_agent import run_agentic

                        chronicle, _arb = run_agentic(
                            chronicler, text_for_chronicle, vol=vol, ch=ch,
                            payoff=(phase is Phase.TAIL), provider=provider,
                            session=sess, embedding=embedding,
                            max_rounds=agentic_chronicle_rounds,
                        )
                    else:
                        chronicle = chronicler.run(text_for_chronicle, vol, ch,
                                                   payoff=(phase is Phase.TAIL))
                    events = chronicle.written
    except Exception:  # noqa: BLE001 - 编纂失败不影响草稿已落盘
        chronicle = None
        soft_failures.append("chronicle(事件回写失败)")

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

    # ---- 5.5) 定时事项记账（ADR-019 §3.3.3，M3m T2）----
    # 章末统一判定：正文 token 命中 → fired（解除不可出场期）；到期未兑现 →
    # overdue 递增（≥3 warn / ≥5 软 block / block×3 自动 expired）。失败不影响成稿。
    pending_tick: dict = {}
    try:
        from .timeline import tick as _timeline_tick

        verify_text = draft.read_text(encoding="utf-8") if draft.exists() else final
        pending_tick = _timeline_tick(ws, project_id, vol=vol, ch=ch,
                                      chapter_text=verify_text).as_dict()
    except Exception:  # noqa: BLE001 - 记账失败不影响成稿
        pending_tick = {}

    # ---- 5.6) 实然关系账本（ADR-023 A2 + D3-b）----
    # 章末确定性重建 pair 账本（可再生投影，零新增 LLM）；阈值触发的翻转
    # （同向×2 / 断裂级 / 复合级）生成"关系修订提案"入 enrich pending，
    # 人工 --allow 才改写 bible 应然行（绝不在管线内自动覆盖）。
    try:
        from .rel_ledger import enqueue_flip_proposals, rebuild_ledger

        _rel_ledger = rebuild_ledger(ws, project_id)
        rel_pairs = int((_rel_ledger.get("meta") or {}).get("count") or 0)
        rel_proposals = len(enqueue_flip_proposals(ws, project_id, _rel_ledger))
    except Exception:  # noqa: BLE001 - 账本失败不影响成稿
        rel_pairs = 0
        rel_proposals = 0

    # F7.1：正文生成审计（reports/ + .index.db audit_log）
    _write_generation_audit(ws, project_id, vol, ch, provider, ok=True, mode=mode,
                            events=events, phase=getattr(phase, "value", phase),
                            jit=jit_added, settings_pending=settings_pending,
                            world_now=pending_tick.get("now"),
                            rel_pairs=rel_pairs, rel_proposals=rel_proposals)

    # ---- ADR-030（M3z 批次 B，F2.6）：成稿后落草稿源清单快照 ----
    # 依赖"生成时装配的 prompt 指纹 + 本章最终正文"，供人工改稿后修订归因（ADR-031）。
    # 单独 try：溯源失败绝不影响草稿已落盘（H1 增强层纪律）。
    try:
        if final or draft.exists():
            from .draft_provenance import build_source_list, write_source_list

            _final_txt = final if mode == "direct" else (
                draft.read_text(encoding="utf-8") if draft.exists() else final)
            _src = build_source_list(ws, project_id, vol, ch, content=_final_txt,
                                     system_prompt=system_prompt or "", goal=goal,
                                     provider=provider, mode=mode)
            write_source_list(ws, project_id, vol, ch, _src)
    except Exception:  # noqa: BLE001 - 溯源失败不影响成稿
        pass

    return ProductionResult(ok=True, chapter_path=str(draft), result=final, events_committed=events,
                            mode=mode, bible_injected=bible_injected, attempts=attempts,
                            completeness=comp, polish=polish_res, chronicle=chronicle,
                            review_blocks=review_blocks, events_revised=events_revised,
                            lessons_added=lessons_added, jit_added=jit_added,
                            settings_pending=settings_pending,
                            factory_added=factory_added,
                            entity_new=entity_new, entity_alerts=entity_alerts,
                            phase=getattr(phase, "value", phase), phase_reason=phase_reason,
                            events_capped=events_capped,
                            first_seen_patched=first_seen_patched,
                            volume_facts_built=volume_facts_built,
                            seam_hits=seam_hits,
                            pending_tick=pending_tick,
                            chapter_title=chapter_title, directions_built=directions_built,
                            perspectives_written=perspectives_written,
                            broadcasts_built=broadcasts_built,
                            rel_pairs=rel_pairs, rel_proposals=rel_proposals,
                            soft_failures=soft_failures)
