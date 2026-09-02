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
                 review_blocks: int = 0, events_revised: int = 0, lessons_added: int = 0,
                 jit_added: int = 0, settings_added: int = 0,
                 entity_new: int = 0, entity_alerts: list[str] | None = None,
                 phase: str = "writing", phase_reason: str = "",
                 length_truncated: bool = False, events_capped: int = 0,
                 pending_tick: dict | None = None,
                 chapter_title: str = "", directions_built: int = 0,
                 perspectives_written: int = 0):
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
        self.settings_added = settings_added    # 本轮设定补充数（递归分层 B）
        self.entity_new = entity_new            # 本轮新增实体数（统一实体追踪）
        self.entity_alerts = entity_alerts or []  # 本轮实体预算告警
        self.phase = phase                      # 本卷阶段：opening | writing | tail（第九批）
        self.phase_reason = phase_reason        # 阶段判定依据（确定性规则可解释）
        self.length_truncated = length_truncated  # 篇幅硬上限截断（第七批）
        self.events_capped = events_capped        # 每章事件数超限截掉的事件数（第二批）
        self.pending_tick = pending_tick or {}    # 定时事项记账（ADR-019，M3m T2）
        self.chapter_title = chapter_title        # 延迟拟定的章节标题（ADR-020 决策一）
        self.directions_built = directions_built  # 本轮生成的人物调度单数（ADR-020 决策三）
        self.perspectives_written = perspectives_written  # 本轮写入的角色视角条数（决策四）

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
    # 重复（前两章归因 P0）：此前只查截断与元叙事，整段重复 4 次也 0 告警。
    # 阈值：整段重复 ≥1 或整句重复 ≥3 才算问题——单句重复 1-2 次可能是有意的复沓。
    if comp.get("dup_paragraphs", 0) >= 1:
        problems.append(f"正文有 {comp['dup_paragraphs']} 处整段重复，必须删除多余份")
    elif comp.get("dup_sentences", 0) >= 3:
        problems.append(f"正文有 {comp['dup_sentences']} 处整句重复，必须删除多余份")
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
                   max_tokens_out=budget, max_content_tokens=content_tokens)
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
                max_tokens_out=budget, max_content_tokens=content_tokens)
        )
        piece = (res.content or "").strip()
        if not piece:
            break
        # 续写同样会把已写部分复述一遍（同根因）——确定性去重后再拼接。
        # 注意：续写可能断在**句中**（"他走到山门前，正要/踏入那扇朱红大门。"），
        # 必须无缝拼接，不能加段落分隔——那是事件间 join（"\n\n".join(pieces)）的职责。
        text = text.rstrip() + strip_seam_overlap(text, piece).lstrip()
    return text


def _truncate_to_boundary(text: str, cap: int) -> str:
    """篇幅硬上限（第七批）：超限时截断到最近的段落/句子边界，不在句中腰斩。

    优先级：空行（\\n\\n）> 行末（\\n）> 句末标点；按序取第一个满足的最近边界。
    未超限原样返回；无合适边界（都在前半段）时硬切兜底。上限按字符计。
    """
    if cap is None or cap <= 0 or len(text) <= cap:
        return text
    head = text[:cap]
    for marker in ("\n\n", "\n", "。", "！", "？", "…", "；", "，"):
        pos = head.rfind(marker)
        if pos > cap * 0.5:  # 边界至少要落在后半段，避免切在开头
            return head[: pos + len(marker)]
    return head  # 无合适边界 → 硬切兜底


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


def _recent_chapter_memory(ws, project_id: str, vol: int, ch: int, max_items: int = 3) -> list[str]:
    """最近 1 章固定回退（讨论第 8 轮·用户拍板）：相关检索召回的是语义相似事件，
    未必是时间上最近的事件——长卷下模型容易忘了刚发生的事。直接带最近一章事件。
    """
    try:
        import json as _json

        p = ws._abs(f"{project_id}/memory/plot_events.json")
        if not p.exists():
            return []
        events = _json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    prev = [(e, (e.get("at") or {}).get("ch", 0)) for e in events
            if isinstance(e, dict) and int((e.get("at") or {}).get("vol", 0) or 0) == vol
            and int((e.get("at") or {}).get("ch", 0) or 0) < ch]
    if not prev:
        return []
    latest_ch = max(c for _, c in prev)
    out = [f"- [最近 {latest_ch} 章] {e.get('summary', '')}"
           for e, c in prev if c == latest_ch]
    return out[:max_items]


# ---------------------------------------------------------------- 递归分层 B：世界观滚动补充

def _known_setting_terms(ws, project_id: str) -> set[str]:
    """已知设定名词集合（人物名/别名/地名/设定条目关键词/注册表规范名）。"""
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
    """世界观滚动补充（递归分层 B）：事件文本中出现的新专有名词 → LLM 补 settings 条目。

    与首次交代状态机互补：状态机管"已建条目何时交代"，这里管"条目本身何时补全"。
    失败静默返回 0。
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
            max_tokens_out=400, temperature=0.3, response_format="json_object"))
        if res.blocked or not (res.content or "").strip():
            return 0
        data = _json.loads(res.content.strip())
        if not isinstance(data, list):
            return 0
        path = ws._abs(f"{project_id}/bible/settings.json")
        entries = _json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        n = 0
        for item in data:
            if not isinstance(item, dict) or not item.get("term"):
                continue
            term = str(item["term"]).strip()
            if not term or term in known or any(e.get("term") == term for e in entries):
                continue
            entries.append({
                "id": f"setting:jit{len(entries) + 1}",
                "term": term,
                "text": str(item.get("text") or f"关于{term}的设定")[:80],
                "keywords": [str(k) for k in (item.get("keywords") or []) if k][:4],
                "revealed": False,
            })
            n += 1
        if n:
            path.write_text(_json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
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
            max_tokens_out=200, temperature=0.4))
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


def _prior_chapter_text(ws, project_id: str, vol: int, ch: int, max_chars: int = 1200) -> str:
    """回读机制（第七批第 4 条·用户拍板）：取前 1 章正文**原文**尾部（非摘要）。

    记忆摘要会丢细节（"赵铁山被问话时手抖了一下"这类伏笔级细节），
    回读原文补上；与最近章记忆回退互补——原文给细节、摘要给跨章语义。
    """
    try:
        prev_ch = ch - 1
        if prev_ch < 1:
            return ""
        for base in (ws.chapter_path, ws.draft_path):
            p = base(project_id, vol, prev_ch)
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
                extra_readback: list[str] | None = None) -> str:
    """装配单个事件的生成目标（细纲要点 + 人物调度 + 接缝上下文 + 先忆 + 设定 + RAG）。

    `related`：知识层检索结果注入行（讨论第 8 轮 RAG）。
    `readback_text`：前章正文原文（回读机制），只在第一个事件注入。
    `cast_lines` / `hist_lines` / `direction_lines` / `extra_readback`（ADR-020）：
    出场人物卡（无条件注入）、角色视角近况、人物调度单、久未出场角色的原文回读。
    """
    parts = [f"{chapter_goal}", "",
             f"【本步骤】只撰写本章第 {idx}/{total} 个事件：【{ev_text}】"]
    # 延迟拟题（ADR-020 决策一）：事件级一律不输出标题，标题在整章拼完后统一拟
    parts += ["", "【输出纪律】直接写正文，**不要写章节标题**、不要写「第X章」字样；"
                  "不要重复上文已经写过的内容。"]
    if direction_lines:
        parts += ["", "【本场人物表演指令】（逐人遵守，违反即人物崩坏；"
                      "各人的语气与取舍必须彼此不同）：", *direction_lines]
    if cast_lines:
        parts += ["", "【本场出场人物】（严格按各自的人设写：性格、称谓、立场、"
                      "修为、与其他人的关系都要对得上）：", *cast_lines]
    if hist_lines:
        parts += ["", "【人物近况】（他们带着这些经历进入本场，言行要与之一致）：",
                  *hist_lines]
    if readback_text and idx == 1:
        parts += ["", readback_text]
    if extra_readback:
        parts += ["", *extra_readback]
    if prev_piece:
        parts += ["", f"【上文接缝】（从下面这段的结尾自然续写，不要重复已有内容）：",
                  "…" + prev_piece[-seam_chars:]]
    if memories:
        parts += ["", "【相关前情】（先忆，保持一致）：", *memories]
    if setting_lines:
        parts += ["", "【本事件首次出现的设定】（以下设定此前未在正文交代过，"
                      "必须在本次事件里自然带出，让读者第一次见到就明白：）", *setting_lines]
    if related:
        char_lines = related.get("character") or []
        if char_lines:
            parts += ["", "【本事件相关人物】（按其性格/弧线/当前状态写）：", *char_lines]
        set_lines = related.get("setting") or []
        if set_lines:
            parts += ["", "【相关知识·设定】（与本次事件相关的世界设定，须一致）：", *set_lines]
        for key, label in (("thread", "伏笔"), ("lesson", "教训"), ("faction", "势力")):
            ls = related.get(key) or []
            if ls:
                parts += ["", f"【相关知识·{label}】（本事件相关的{label}，保持连续）：", *ls]
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


def _load_entity_tracker(ws, project_id: str):
    """加载统一实体追踪层（第八批）；失败返回 None（不启用，向后兼容）。"""
    try:
        from .entity import EntityTracker

        return EntityTracker.load(ws, project_id)
    except Exception:  # noqa: BLE001
        return None


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
    """人物 JIT 补卡（递归分层 A）：细纲声明出场但 bible 缺卡 → LLM 补全并写入。

    - 补卡吸收前文实际发展（arc 基于已有事件滚动，而非 setup 时预测）；
    - 补卡走 schema（id/name/status/gender/power/arc/first_appear…）；
    - 失败静默返回 0（不阻断生成）。
    """
    names = parse_cast_decl(gist_text)
    if not names or provider is None:
        return 0
    try:
        import json as _json

        chars_path = ws._abs(f"{project_id}/bible/characters.json")
        chars = []
        if chars_path.exists():
            chars = _json.loads(chars_path.read_text(encoding="utf-8")) or []
        existing = set()
        for c in chars if isinstance(chars, list) else []:
            if isinstance(c, dict) and c.get("name"):
                existing.add(str(c["name"]))
                for a in (c.get("aliases") or []):
                    existing.add(str(a))
        missing = [n for n in names if n not in existing]
        if not missing:
            return 0

        # 前情摘要：让补卡吸收前文实际发展（滚动设计）
        recall = _recall_for(ws, project_id, "，".join(missing), None, top_k=3)
        recall_block = "\n".join(recall) if recall else "（无前情记录，人物首秀）"
        prompt = (
            f"你是人物设定师。为下面的角色补全人物卡（他们将在第 {vol} 卷第 {ch} 章首次出场）："
            f"【{'、'.join(missing)}】\n"
            f"前情（已发生的事件，人物卡须与之不冲突）：\n{recall_block}\n\n"
            f"输出 JSON 数组，每个元素一张人物卡，字段：\n"
            f'{{"name": "角色名", "gender": "male|female|unknown", "age": 数字或null, '
            f'"species": "种族（如人族/妖族）", "core_traits": ["性格1","性格2"], '
            f'"power": {{"level": "境界或实力", "faction": "阵营"}}, '
            f'"arc": "一句话人物弧线（基于前情，可滚动）", '
            f'"first_appear": {{"vol": {vol}, "ch": {ch}}}, '
            f'"status": "active", "aliases": []}}\n'
            f"只输出 JSON 数组，不要解释。"
        )
        res = provider.complete(LLMRequest(
            messages=[LLMMessage(role="user", content=prompt)],
            max_tokens_out=800, temperature=0.5, response_format="json_object"))
        if res.blocked or not (res.content or "").strip():
            return 0
        data = _json.loads(res.content.strip())
        if not isinstance(data, list):
            data = [data]
        n = 0
        for card in data:
            if not isinstance(card, dict) or not card.get("name"):
                continue
            if str(card["name"]) in existing:
                continue
            cid = f"char:jit{len(chars) + n + 1}"
            new_card = {
                "id": cid,
                "name": str(card["name"]),
                "aliases": [str(a) for a in (card.get("aliases") or [])],
                "gender": card.get("gender") if card.get("gender") in ("male", "female") else "unknown",
                "species": str(card.get("species") or "人族"),
                "age": card.get("age"),
                "core_traits": [str(x) for x in (card.get("core_traits") or [])][:5],
                "power": {"level": str(card.get("power", {}).get("level") or ""),
                          "faction": str(card.get("power", {}).get("faction") or "")},
                "arc": str(card.get("arc") or ""),
                "first_appear": {"vol": vol, "ch": ch},
                "status": "active",
                "relationships": [],
            }
            chars.append(new_card)
            existing.add(str(card["name"]))
            n += 1
        if n:
            chars_path.write_text(_json.dumps(chars, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
            # 新人物进 worldstate（init_from_bible 不覆盖已有状态）
            try:
                from .worldstate import init_from_bible

                init_from_bible(ws, project_id)
            except Exception:  # noqa: BLE001
                pass
        return n
    except Exception:  # noqa: BLE001 - 补卡失败不阻断生成
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
                            phase: str = "", note: str = "", jit: int = 0, settings: int = 0) -> None:
    """F7.1：正文生成审计双落——`reports/stats/generation-<ts>.md`（人读持久）+ `.index.db`
    `audit_log`（机器查，ADR-016 辅助索引可重建）。先落报告文件、后同步索引（ADR-016
    写操作先文件后索引）；审计失败静默——审计不该阻断写章。

    计价与 forge 报告同口径（¥1/1M in + ¥2/1M out，DeepSeek 参考价）。
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
            f"- 事件回写：{events} 条；JIT 补卡 {jit}；设定补充 {settings}；阶段 {phase or '-'}",
            f"- LLM 调用 {counter.calls} 次：in={counter.tokens_in} / out={counter.tokens_out} tokens，"
            f"估算成本 ¥{cost:.4f}",
        ]
        ws.write_text(p, "\n".join(lines) + "\n")
        from ..storage.indexdb import IndexDb

        db = IndexDb(str(ws.index_db_path(project_id)))
        db.init()
        db.write_audit("produce_chapter", f"{vol}-{ch}",
                       _json.dumps({
                           "ok": ok, "mode": mode, "events": events, "calls": counter.calls,
                           "tokens_in": counter.tokens_in, "tokens_out": counter.tokens_out,
                           "cost": round(cost, 6), "phase": phase, "jit": jit,
                           "settings": settings, "note": note, "ts": ts,
                       }, ensure_ascii=False))
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
    length_cap_chars: int | None = None,  # 篇幅硬上限（字符）；None = 不截断
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
    # F7.1：整章 LLM 用量聚合（含嵌套函数与编纂），出口统一写审计
    provider = _UsageCounter(provider)

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

    # 递归分层 A（第七批第 5 条·用户拍板）：人物 JIT 补卡——细纲声明出场但 bible 缺卡，
    # 生成前先补全（出场即建档，防造人红线）。补卡吸收前文实际发展（滚动设计）。
    jit_added = 0
    settings_added = 0
    if jit_characters:
        jit_added = _jit_characters(ws, project_id, vol, ch, gist_text_for_events, provider)
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
                settings_idx = (settings if settings is not None
                                else _load_settings(ws, project_id))
                # 知识检索层（讨论第 8 轮 RAG）：每章构建一次（语义向量化秒级），
                # 每事件按 LLM 生成的查询 + 事件文本检索相关知识注入
                knowledge = _make_knowledge(ws, project_id, embedding)
                readback_text = (_prior_chapter_text(ws, project_id, vol, ch)
                                 if readback else "")
                # ADR-020：人物层三件套的一次性准备（圣经人物卡每章读一次；
                # 细纲出场声明每章解析一次，事件循环内只做匹配）
                from . import director as _director
                bible_chars = _director.load_characters(ws, project_id) if any(
                    (cast_injection, character_direction, perspective_memory)) else []
                declared_cast = parse_cast_decl(gist_text_for_events)
                # 统一实体追踪（第八批：四阶段 + 别名消歧 + 松预算）
                entity_tracker = entity_tracker or _load_entity_tracker(ws, project_id)
                for idx, ev_text in enumerate(key_events, 1):
                    is_last = idx == len(key_events)
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
                        cast_cards = _director.match_cast(bible_chars, declared_cast) \
                            if declared_cast else []
                        # 细纲声明为准，再补事件文本里出现但声明漏掉的（≤2 人，防噪声）
                        extra = [c for c in _director.cast_from_text(bible_chars, ev_text)
                                 if c["id"] not in {x["id"] for x in cast_cards}]
                        cast_cards = (cast_cards + extra[:2]) if cast_cards \
                            else _director.cast_from_text(bible_chars, ev_text)
                        cast_names = [str(c.get("name") or "") for c in cast_cards]
                        if cast_injection:
                            cast_lines = _director.render_cards(cast_cards)
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
                        if character_direction:
                            sheet = _director.build_direction(
                                ws, project_id, provider, vol=vol, ch=ch,
                                event_index=idx, ev_text=ev_text, cards=cast_cards,
                                history_lines=hist_lines, system_prompt=system_prompt)
                            if sheet is not None:
                                direction_lines = sheet.lines()
                                _director.save_direction(ws, project_id, sheet)
                                directions_built += 1
                    prompt = _event_goal(goal, ev_text, idx, len(key_events),
                                         pieces[-1] if pieces else "", seam,
                                         memories_ev, is_last, setting_lines, related,
                                         readback_text, cast_lines=cast_lines,
                                         hist_lines=hist_lines,
                                         direction_lines=direction_lines,
                                         extra_readback=extra_rb)

                    # 递归分层 B（第七批第 5 条·用户拍板）：世界观滚动补充——
                    # 事件文本出现新专有名词 → LLM 补 settings 条目（后续事件可命中）。
                    if supplement_settings:
                        try:
                            settings_added += _supplement_settings(ws, project_id, ev_text,
                                                                   provider)
                        except Exception:  # noqa: BLE001
                            pass

                    # 递归分层 C（第七批第 5 条·用户拍板）：重场戏拍展开——
                    # 细纲事件标 [expanded] → 拆 ≤3 拍逐拍生成；任何拍失败回退事件级。
                    beats_used = 0
                    if is_expanded_event(ev_text):
                        beat_res = _generate_beats(
                            provider, system_prompt or "", goal,
                            _strip_expanded_tag(ev_text),
                            memories_ev, setting_lines, related, readback_text,
                            generation_tokens, max_continuations, direct_words_floor,
                            content_tokens=content_tokens)
                        if beat_res is not None:
                            piece, beats_used = beat_res

                    if not beats_used:
                        piece = _generate_with_continuation(
                            provider, system_prompt or "", prompt, generation_tokens,
                            max_continuations, content_tokens=content_tokens)
                    # 单事件最小篇幅（第二批·人工审查）：低于下限即失败重试，
                    # 避免"草草两句话一个事件"稀释正文密度
                    event_floor = max(direct_words_floor, min_event_words or 0)
                    if len(piece) < event_floor:
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
                final = _dedupe_chapter_titles("\n\n".join(pieces))
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
        _write_generation_audit(ws, project_id, vol, ch, provider, ok=False, mode=mode,
                                phase=getattr(phase, "value", phase),
                                note=f"生成异常：{str(e)[:60]}")
        return ProductionResult(ok=False, result=str(e), mode=mode, bible_injected=bible_injected,
                                attempts=attempts, completeness=comp,
                                phase=getattr(phase, "value", phase), phase_reason=phase_reason,
                                events_capped=events_capped)

    if not comp and final:
        comp = completeness(final)

    # ---- 3) 落盘草稿 ----
    draft = ws.draft_path(project_id, vol, ch)
    if mode == "direct":
        try:
            draft.parent.mkdir(parents=True, exist_ok=True)
            ws.write_text(draft, final + "\n")
        except Exception:  # noqa: BLE001
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
    if polish and mode == "direct":
        try:
            polish_res = polish_chapter(final, provider, vol=vol, ch=ch,
                                        tone=_style_tone(ws, project_id),
                                        is_chapter=True,
                                        system_prompt=system_prompt)
            if polish_res.changed:
                final = polish_res.text
                try:
                    ws.write_text(draft, final + "\n")
                except OSError:  # pragma: no cover
                    polish_res = None
        except Exception:  # noqa: BLE001 - 润色失败不阻断，保留原稿
            polish_res = None

    # ---- 4.4) 篇幅硬上限（第七批·用户拍板）----
    # 超限截断到段落边界（不在句中腰斩），止损膨胀失控（v5 ch16 曾 7890 字）；
    # 截断标记进 result，CLI 可见。截断在 polish 之后——润色可能加长。
    length_truncated = False
    if length_cap_chars and final and mode == "direct":
        capped = _truncate_to_boundary(final, length_cap_chars)
        if len(capped) < len(final):
            length_truncated = True
            final = capped
            if mode == "direct":
                try:
                    ws.write_text(draft, final + "\n")
                except OSError:  # pragma: no cover
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
                if chronicler is None and inject_bible is not False:
                    from .chronicler import Chronicler

                    chronicler = Chronicler(ws, project_id, llm=provider, embedding=embedding,
                                            semantic_checker=semantic_checker)
                if chronicler is not None:
                    chronicle = chronicler.run(text_for_chronicle, vol, ch,
                                               payoff=(phase is Phase.TAIL))
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

    # F7.1：正文生成审计（reports/ + .index.db audit_log）
    _write_generation_audit(ws, project_id, vol, ch, provider, ok=True, mode=mode,
                            events=events, phase=getattr(phase, "value", phase),
                            jit=jit_added, settings=settings_added)

    return ProductionResult(ok=True, chapter_path=str(draft), result=final, events_committed=events,
                            mode=mode, bible_injected=bible_injected, attempts=attempts,
                            completeness=comp, polish=polish_res, chronicle=chronicle,
                            review_blocks=review_blocks, events_revised=events_revised,
                            lessons_added=lessons_added, jit_added=jit_added,
                            settings_added=settings_added,
                            entity_new=entity_new, entity_alerts=entity_alerts,
                            phase=getattr(phase, "value", phase), phase_reason=phase_reason,
                            length_truncated=length_truncated, events_capped=events_capped,
                            pending_tick=pending_tick,
                            chapter_title=chapter_title, directions_built=directions_built,
                            perspectives_written=perspectives_written)
