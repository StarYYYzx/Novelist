"""文风度量与优化（B-08 文风维度 + "AI 味"治理）。

## 为什么先做度量

"AI 味太重"是个主观判断，不量化就无法证明润色有没有用。本模块先给出**确定性**的
AI 味指标（纯正则统计，不调 LLM），再据此驱动润色、并用同一指标验证前后差异。

## AI 味的九种可测信号

中文大模型散文的典型腔调，逐条对应一个已观察到的写作习惯：

| 信号 | 例 |
| --- | --- |
| 对比排比 | 不是…而是… |
| 模糊比喻 | 仿佛 / 似乎 / 宛如 / 犹如 |
| 「X 如 Y」模板 | 锐利如刀、深邃如渊 |
| 时间套话 | 这一刻 / 从这一刻起 / 与此同时 |
| 认知动词开头 | 他知道 / 她意识到 / 他明白 |
| 破折号滥用 | —— 密度过高 |
| 节奏过匀 | 各段字数标准差极小（模型偏好等长段落） |
| 结尾升华 | 末段出现「从这一刻起 / 不再 / 真正的」 |
| 形容词堆叠 | 连续「…的…的…」 |

`ai_tone_score()` 返回每千字的命中密度与一个 0–100 的综合分（越低越好）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict

from .llm import LLMMessage, LLMRequest


@dataclass
class StyleMetrics:
    """一章的文风度量。"""

    chars: int = 0
    paragraphs: int = 0
    para_len_stdev: float = 0.0
    signals: dict[str, int] = field(default_factory=dict)
    per_1k: dict[str, float] = field(default_factory=dict)
    score: float = 0.0          # 0–100，越低越不像 AI

    def to_dict(self) -> dict:
        return asdict(self)


# 信号名 -> 正则
SIGNAL_PATTERNS: dict[str, str] = {
    "对比排比": r"不是[^。！？]{0,18}而是",
    "模糊比喻": r"仿佛|似乎|宛如|犹如|宛若",
    "如字模板": r"[一-龥]{1,4}如[一-龥]{1,3}(?:般|一样|似的)?",
    "时间套话": r"这一刻|从这一刻起|就在此时|与此同时|刹那间",
    "认知开头": r"(?:^|[。！？\n])\s*(?:他知道|她知道|他明白|她明白|他意识到|她意识到|他忽然明白)",
    "破折号": r"——",
    "省略号": r"…{2,}|…",
    "形容词堆叠": r"[一-龥]{2,4}的[一-龥]{2,4}的[一-龥]{2,4}",
    "结尾升华": r"从这一刻起|不再是|真正的|终将|注定",
}

# 各信号权重（对"AI 味"的贡献度；纯经验值，可按书调）
SIGNAL_WEIGHTS: dict[str, float] = {
    "对比排比": 1.4,
    "模糊比喻": 1.2,
    "如字模板": 1.3,
    "时间套话": 1.1,
    "认知开头": 0.9,
    "破折号": 0.6,
    "省略号": 0.5,
    "形容词堆叠": 1.0,
    "结尾升华": 0.8,
}

_END = "。！？」）】…》”’\"'"


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in text.splitlines() if p.strip()]


def measure(text: str) -> StyleMetrics:
    """统计一段正文的 AI 味信号。纯确定性，不调 LLM。"""
    m = StyleMetrics(chars=len(text))
    paras = _paragraphs(text)
    m.paragraphs = len(paras)

    if len(paras) >= 2:
        lens = [len(p) for p in paras]
        avg = sum(lens) / len(lens)
        var = sum((x - avg) ** 2 for x in lens) / len(lens)
        m.para_len_stdev = round(var ** 0.5, 2)

    k = max(1, len(text) / 1000)
    for name, pat in SIGNAL_PATTERNS.items():
        n = len(re.findall(pat, text))
        m.signals[name] = n
        m.per_1k[name] = round(n / k, 2)

    raw = sum(m.per_1k.get(name, 0.0) * w for name, w in SIGNAL_WEIGHTS.items())
    # 节奏惩罚：段落长度过于均匀（标准差 < 25 字）是模型散文的典型特征
    rhythm_penalty = 6.0 if (len(paras) >= 4 and m.para_len_stdev < 25) else 0.0
    m.score = round(min(100.0, raw * 4.0 + rhythm_penalty), 2)
    return m


def _dup_stats(lines: list[str]) -> tuple[int, int]:
    """重复检测（前两章归因 P0）：返回 (重复段落数, 重复句数)。

    只做**确定性**判定：段落去空白后完全相同 → 重复段；
    句子（≥12 字）在全文出现 2 次以上 → 重复句（按多余次数累计）。
    不做相似度——相似度会把"他点点头"这类正常复现也算成重复。
    """
    seen_para: dict[str, int] = {}
    for ln in lines:
        key = re.sub(r"\s+", "", ln)
        if len(key) >= 8:
            seen_para[key] = seen_para.get(key, 0) + 1
    dup_paras = sum(n - 1 for n in seen_para.values() if n > 1)

    sents: dict[str, int] = {}
    for ln in lines:
        for s in re.findall(r"[^。！？；\n]+[。！？；]?", ln):
            key = re.sub(r"\s+", "", s)
            if len(key) >= 12:
                sents[key] = sents.get(key, 0) + 1
    dup_sents = sum(n - 1 for n in sents.values() if n > 1)
    return dup_paras, dup_sents


# ---------------------------------------------------------------- 近重复（事件重演）

# 近重复判定参数（ch7/ch8 实证标定，见 tests/test_chapter_dedup.py）：
# 事件重演的典型形态是"后文用不同措辞把已写过的收尾再写一遍"——精确比对查不出，
# 必须用字符 n-gram 覆盖率。阈值 0.62 是实测分界：真实重演 0.67~1.0，
# 正常行文（同一人物、不同动作）最高 0.4 左右；低于 0.62 会把正常叙述误杀。
NEAR_DUP_THRESHOLD = 0.62
NEAR_DUP_MIN_CHARS = 15
_NEAR_DUP_N = 3


def _gram_coverage(a: str, b: str, n: int = _NEAR_DUP_N) -> float:
    """`a` 的字符 n-gram 被 `b` 覆盖的比例（对称归一化：除以较小的 gram 集）。

    分母取 **min(|A|,|B|)** 而非 |A|：重写时模型常增删修饰语使句子变长，
    用 |A| 做分母会把"加了几字但内容相同"的复述稀释到阈值以下。
    """
    A = {a[i:i + n] for i in range(len(a) - n + 1)}
    B = {b[i:i + n] for i in range(len(b) - n + 1)}
    if not A or not B:
        return 0.0
    return len(A & B) / min(len(A), len(B))


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def find_near_dup_paragraphs(text: str, threshold: float = NEAR_DUP_THRESHOLD,
                             min_chars: int = NEAR_DUP_MIN_CHARS
                             ) -> list[tuple[int, int, float]]:
    """找出"内容高度相似但不完全相同"的段落对（事件重演/结尾段复制）。

    返回 `[(i, j, sim)]`（i < j，行号 0 基，按正文行计）。**精确相同**的段落不计入
    ——那归 `_dup_stats` 的 `dup_paragraphs` 管，避免重复报数。
    """
    paras = [_norm(p) for p in _paragraphs(text)]
    out: list[tuple[int, int, float]] = []
    for i in range(len(paras)):
        for j in range(i + 1, len(paras)):
            if len(paras[i]) < min_chars or len(paras[j]) < min_chars:
                continue
            if paras[i] == paras[j]:
                continue
            sim = _gram_coverage(paras[i], paras[j])
            if sim >= threshold:
                out.append((i, j, round(sim, 3)))
    return out


def _is_dialogue(s: str) -> bool:
    """整句是否是对白（去空白后包在 “” 内）。对白允许复现（呼应/口头禅），不删。"""
    return len(s) >= 2 and s[0] in "“\"" and s[-1] in "”\""


def strip_near_dup_sentences(text: str, threshold: float = NEAR_DUP_THRESHOLD,
                             min_chars: int = NEAR_DUP_MIN_CHARS) -> tuple[str, int]:
    """删掉后文里与前文近重复的句子，返回 (新正文, 删除句数)。

    **为什么删句子而不是删段落**：重演的后一段往往夹带一两句新信息
    （ch8 实证：重演段首句"段无涯转身离去，背影孤傲而决绝"是新的，
    后面整段才是复制）。整段删除会误杀新内容，句级删除能只切掉复制部分。

    判定：句子（去空白 ≥ `min_chars` 字）与**任意**前文句子的 n-gram 覆盖率
    ≥ `threshold` 即删。两类句子**不删**（ch6/ch7 实证，误删代价高于漏删）：

    1. **对白句**（整句包在 “” 内）：ch6 实证老夫先后两次说"五五开……三个字，
       老夫听过"，后者是**有意的呼应/回收**，n-gram 相似度 0.857 比真正的重演
       还高——靠阈值分不开，只能靠"是不是对白"分。
    2. **系统面板行**（`【…】` 开头）：面板文本本就允许逐字复现；且切分时会把
       行尾的 `】` 切出去，删半截会留下孤零零的 `】`。整行按原子处理。

    标题行（`#` 开头）与短行（< `min_chars`）同样不参与比较也不删除。
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    seen: list[str] = []
    kept: list[str] = []
    removed = 0
    for ln in lines:
        raw = ln.strip()
        if not raw:
            kept.append(ln)
            continue
        if raw.startswith("#") or len(_norm(raw)) < min_chars:
            # 标题与短行（单句成段、拟声、单句对白）不进比较池，也不删
            kept.append(ln)
            continue
        if raw.startswith("【"):
            kept.append(ln)          # 面板行按原子保留，不切不删
            seen.append(_norm(raw))
            continue
        is_dialogue_line = _is_dialogue(_norm(raw))
        parts = re.findall(r"[^。！？；\n]+[。！？；]?", raw)
        kept_parts: list[str] = []
        for p in parts:
            key = _norm(p)
            if len(key) < min_chars:
                kept_parts.append(p)
                continue
            # 整行包在引号里 → 行内每一句都算对白（ch6 实证：呼应句在引号内的第二句，
            # 只看单句会漏判，把"老夫听过"的回收当成重演删掉）
            if is_dialogue_line or _is_dialogue(key):
                kept_parts.append(p)   # 对白：呼应与口头禅允许复现
                continue
            # 同一句话在**本行内**重复出现也算（模型偶发句内复制）
            if any(_gram_coverage(key, prev) >= threshold for prev in seen):
                removed += 1
                continue
            kept_parts.append(p)
            seen.append(key)
        new_line = "".join(kept_parts).strip()
        if new_line:
            kept.append(new_line)
    if removed == 0:
        return text, 0  # 干净文本原样返回：不因换行归一化改动一个字
    # 删段会留下连续空行（原段落连同它前后的分隔符一起消失）→ 收敛成单个空行
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)), removed


def completeness(text: str) -> dict:
    """生成完整性检查（B-04）：截断 / 元叙事泄漏 / 篇幅 / **重复**。"""
    stripped = text.strip()
    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    last = lines[-1] if lines else ""
    # 元叙事只查正文，**跳过第一行**——那一行是章节标题，本来就该写「第X章」。
    # 真正的泄漏是"那是他在第一章捡到的"这类出现在叙述里的说法。
    body = "\n".join(lines[1:]) if len(lines) > 1 else ""
    meta = re.findall(r"第\s*[一二三四五六七八九十\d]+\s*章|本章|上一章|下一章|细纲|大纲|前文提要",
                      body)
    # 重复检测（前两章归因 P0）：此前 completeness 只查截断/元叙事，
    # 实测 ch1 有整段重复 4 次却 0 告警——"系统自带 0 告警 ≠ 没问题，只是没检查"。
    dup_paras, dup_sents = _dup_stats(lines)
    # 近重复段落（事件重演/结尾段复制）：ch7/ch8 实证——同一收尾动作写了两遍，
    # 措辞不同故精确比对查不出，日志只报 1~2 处精确重复，人工审读才发现整段重演。
    near = find_near_dup_paragraphs(stripped)
    return {
        "chars": len(stripped),
        "ends_properly": bool(last) and last[-1] in _END,
        "last_char": last[-1] if last else "",
        "meta_narration": sorted(set(meta)),
        "paragraphs": len(lines),
        "dup_paragraphs": dup_paras,
        "dup_sentences": dup_sents,
        "dup_near_paragraphs": len(near),
    }


# ---------------------------------------------------------------- 文风润色


@dataclass
class PolishResult:
    text: str
    before: StyleMetrics
    after: StyleMetrics
    changed: bool = True
    note: str = ""

    @property
    def delta(self) -> float:
        """AI 味分数变化（负数 = 变好）。"""
        return round(self.after.score - self.before.score, 2)


# 语言风格模板（人工审查第七批第 2 条：风格 skill）——style.json.tone 指定，
# 润色 prompt 的【要达到的效果】段据此生成。首版四套，至少覆盖严谨冷肃/诙谐幽默。
TONE_TEMPLATES: dict[str, str] = {
    "严谨冷肃": (
        "【要达到的风格：严谨冷肃】\n"
        "- 句式短促克制，多用陈述句与冷峻动作，少抒情\n"
        "- 情绪藏在动作与细节里，不直接写「他感到恐惧/愤怒」\n"
        "- 修辞克制：一个比喻句顶一个画面，杜绝排比堆砌\n"
        "- 对话简练，符合人物的身份与城府，不留废话\n"
        "- 节奏以顿挫为主：句号多、逗号少，段落短而密"
    ),
    "诙谐幽默": (
        "【要达到的风格：诙谐幽默】\n"
        "- 节奏轻快，允许口语化表达与适度的自我调侃\n"
        "- 用反差制造笑点：正经场景里插入一句不合时宜的内心吐槽\n"
        "- 比喻可俏皮，但必须落在画面里，不落俗套\n"
        "- 对话鲜活，允许俏皮话、双关与语气词，但符合人物性格\n"
        "- 收尾常带余味：一个机锋、一个反转，或一句淡去的玩笑"
    ),
    "热血激昂": (
        "【要达到的风格：热血激昂】\n"
        "- 节奏递进：短句起势，关键处允许长句如浪潮推高\n"
        "- 情绪外放但克制口号腔：用行动与对白的热度代替形容词\n"
        "- 对白有力，关键时刻句子斩钉截铁，喊得出声\n"
        "- 允许必要的排比与顿挫，但只用在情感最高点\n"
        "- 结尾常有上扬的余韵：不是总结，是下一场战斗的号角"
    ),
    "温柔细腻": (
        "【要达到的风格：温柔细腻】\n"
        "- 节奏舒缓，句与句之间留有余白\n"
        "- 感官细节优先：光、气味、触感、温度，用细微之物托住情绪\n"
        "- 情绪隐而不发：写到七分，留三分给读者\n"
        "- 对白含蓄，字少情多，留白胜过直白\n"
        "- 结尾常落在静物或远景上，余韵绵长"
    ),
}


POLISH_RULES = """你要改写下面这一段的**文风**，让它读起来像人写的，而不是大模型生成的。

【绝对不能改的】
- 情节、事件顺序、人物行为动机：一个字都不能改
- 对白内容：可微调语气词，但不得改变意思
- 出场人物姓名与称谓：不得增删人物
- 章节标题（若有，第一行）：原样保留

【必须消除的 AI 腔调】(括号内是本段实测命中次数)
- 「不是…而是…」式对比排比（{n_对比排比}）
- 「仿佛 / 似乎 / 宛如」这类模糊比喻（{n_模糊比喻}）
- 「X 如 Y」式比喻模板，如"锐利如刀""深邃如渊"（{n_如字模板}）
- 「这一刻 / 从这一刻起 / 与此同时」这类时间套话（{n_时间套话}）
- 以「他知道 / 他意识到」开头的句子（{n_认知开头}）
- 破折号「——」与省略号「…」的滥用（{n_破折号} / {n_省略号}）
- 连续「…的…的…」形容词堆叠（{n_形容词堆叠}）
- 结尾强行升华（{n_结尾升华}）

【要达到的效果】
- 句式长短交错：有的句子三五字，有的句子二三十字，不要整齐划一
- 段落长短不均：允许出现单句成段，也允许出现较长段落（当前段落长度标准差 {stdev} 字，目标 > 30）
- 多用具体动作、名词、对白推进，少用形容词与心理旁白
- 该省略就省略，不要解释清楚每一个因果
{tone_block}
【输出】只输出改写后的完整正文{heading_tail}。不要写任何说明、注释或前后对比。
"""


def build_polish_prompt(text: str, metrics: StyleMetrics | None = None,
                        tone: str | None = None,
                        is_chapter: bool = False,
                        global_context: str | None = None) -> str:
    """装配润色 prompt：把本段实测到的 AI 味信号次数喂给模型，做定向改写。

    `tone`：style.json.tone 指定的风格（严谨冷肃/诙谐幽默/…），命中模板则追加风格段；
    `is_chapter=False`：事件级润色——不要求章节标题，输出指令相应调整。
    `global_context`（批次三·方案4）：全书视野块（章位置/前章收尾/全局禁令/前卷事实），
    置于原文之前——润色从"逐句美化"升级为"带全局的通读顺稿"。
    """
    m = metrics or measure(text)
    tone_block = ""
    if tone and tone in TONE_TEMPLATES:
        tone_block = "\n" + TONE_TEMPLATES[tone] + "\n"
    # 重复残留（前两章归因 P0）：实测 ch1 有整段重复 4 次，此前润色完全不看重复，
    # 只改文风——重复照原样留在改后文本里。把重复计数直接喂给模型，令其删除多余份。
    dup = completeness(text)
    if dup["dup_paragraphs"] or dup["dup_sentences"] >= 2:
        tone_block += (
            f"\n【必须删除的重复】原文有 {dup['dup_paragraphs']} 处整段重复、"
            f"{dup['dup_sentences']} 处整句重复。这是生成期拼接时的复述残留，不是有意的反复：\n"
            "- 每一处只保留**一次**，其余整段/整句删除；\n"
            "- 删除后不要补写新内容顶替，也不要改写保留的那一份的措辞。\n")
    if global_context:
        tone_block += ("\n" + global_context.strip() + "\n"
                       "（以上全书视野仅供通读顺稿时把握连续性与禁忌，"
                       "其内容本身**不得**出现在改写后的正文里。）\n")
    heading_tail = "，第一行是章节标题" if is_chapter else "（不要加标题，直接给正文片段）"
    rules = POLISH_RULES.format(
        stdev=m.para_len_stdev,
        tone_block=tone_block,
        heading_tail=heading_tail,
        **{f"n_{k}": m.signals.get(k, 0) for k in SIGNAL_PATTERNS},
    )
    return f"{rules}\n原文（第 {m.chars} 字）：\n\n{text}"


def polish_chapter(
    text: str,
    llm,
    *,
    vol: int | None = None,
    ch: int | None = None,
    max_tokens: int = 4000,   # 云篇章 3800 字 2200 token 会截断（M5i 实测）
    tone: str | None = None,
    is_chapter: bool = False,
    system_prompt: str | None = None,
    global_context: str | None = None,
) -> PolishResult:
    """在成章之后**额外追加一次** LLM 调用专门优化文风。

    这是"多次调用换质量"的最后一环：生成时保情节，润色时保情节、改文风。
    润色后用同一套确定性指标复核；若分数反而变差则保留原文（`changed=False`）。

    `tone`：语言风格（style.json.tone）；`is_chapter=True` 用于整章润色（要求保留
    章节标题），False 用于事件级片段润色（不要标题）。
    `system_prompt`（前两章归因 P0）：此前润色调用**不带 system message**，模型只看到
    user 里的改写规则，没有"你是谁、这是什么书"的锚定——实测会丢失人设与文风锚点。
    传入即作为 system message 透传；None 时退回一个通用润色角色设定。
    `global_context`（批次三·方案4）：全书视野块，随 user prompt 注入（见
    build_polish_prompt）。
    """
    before = measure(text)
    if llm is None:
        return PolishResult(text=text, before=before, after=before, changed=False,
                            note="no llm provider bound; skipped")

    sys_msg = system_prompt or (
        "你是中文网文的文字匠，只做文字层面的改写，不改变情节、不增删人物、"
        "不改变人物性格与称谓。忠实于原文发生的每一件事。")
    res = llm.complete(
        LLMRequest(
            messages=[LLMMessage(role="system", content=sys_msg),
                      LLMMessage(role="user",
                                 content=build_polish_prompt(text, before, tone=tone,
                                                             is_chapter=is_chapter,
                                                             global_context=global_context))],
            max_tokens_out=max_tokens,
            temperature=0.6,
        )
    )
    if res.blocked or not (res.content or "").strip():
        return PolishResult(text=text, before=before, after=before, changed=False,
                            note="polish blocked or empty; kept original")

    new_text = res.content.strip()
    after = measure(new_text)

    # 保底：润色不得把章节弄坏（截断，或砍掉大半篇幅）
    comp = completeness(new_text)
    if not comp["ends_properly"] or comp["chars"] < len(text) * 0.35:
        return PolishResult(text=text, before=before, after=before, changed=False,
                            note=f"polish degraded the chapter (chars={comp['chars']}, "
                                 f"ends={comp['ends_properly']}); kept original")
    if after.score > before.score:
        return PolishResult(text=text, before=before, after=after, changed=False,
                            note=f"polish did not improve ({before.score} -> {after.score}); kept original")
    return PolishResult(text=new_text, before=before, after=after, changed=True)
