"""模式二 `ingest`（docs/10 §6）：已有稿子 → 蓝图 + 接着写（M3l F3）。

流程（docs/08 F3 表格）：
1. **摄入与切片**（§6.1）：目录/文件列表（--recursive，.md/.txt）→ 优先 `第X章`/`Chapter N`
   标题行切章，识别不到按空行段落 + 目标字数兜底切；预览确认（回车=全部确认 / `R`=重切 /
   `q`=退出 / `N`=从该章起重切），未确认不写库。
2. **抽取**（§6.2）：确定性优先（人名正则 + 称谓表 + 流派词表 realms/faction_suffix/item_suffix +
   时间量词启发式），LLM 补语义（性别/境界/性格/关系/别名、key_events、pending、伏笔、文风观察）。
   抽取独立配额 `--ingest-max-calls`（默认 30），超出后剩余章降级**纯确定性抽取**，report 披露。
3. **归并消歧**（§6.3）：别名归一合并为同一 `char:` 键（叶岚/叶师弟），按出现频次排序，
   **频次 ≥ 阈值**进主线人物，其余配角（第八批教训：合并错了后面全错）。
4. **文风画像**（§6.4）：确定性指标（句长均值/对白占比/段落长度/语气词频/专名密度），
   tone 由抽取的 style_notes 归纳；provenance=ingested（商讨只 confirm 不重问）。
5. **卷章编码 + 记忆初始化**（§6.5）：N 章按 chapters_per_volume 编入卷 1..k →
   `chapters/<vol>-<ch>.md`（**已是正式章节**）；key_events 反写细纲 `done=true`；
   chronicler 逐章抽事件写 `memory/` + `MemoryIndex.rebuild`（没有这步，第 N+1 章的
   "先忆"是空的）；`EntityTracker.update_from_chapter` 逐章跑（纯正则）重建
   `bible/entity_progress.json`（warm-up，防老人物被当首次登场）；worldstate 初始化为第 N 章末。
6. **缺口检测 → 回落商讨**（§6.6）：detect_gaps 只问缺口槽位（已写内容能推断的已剔除）。

超配额/无 LLM 降级不静默：downgraded 章号 + warnings 披露（docs/10 §12）。
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from ..core.llm import LLMMessage, LLMRequest
from ..storage.workspace import Workspace
from . import genres as _genres
from .ask import ConsultResult, run_consult
from .slots import slots_for_genre
from .state import Blueprint, ForgeState, append_transcript
from .io_console import AnswerIO

DEFAULT_CHAPTERS_PER_VOLUME = 20
DEFAULT_WORDS_PER_CHAPTER = 2400
DEFAULT_INGEST_MAX_CALLS = 30
MAINLINE_FREQ = 3  # 出现频次 ≥ 该值进主线人物（docs/10 §6.3）

# 常见姓氏（人名正则用；收录常用单姓，够启发式用）
_SURNAMES = ("赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
             "戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐费"
             "廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄和穆"
             "萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁杜阮蓝"
             "闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍虞万支柯"
             "管卢莫经房裘缪干解应宗丁宣贲邓郁单杭洪包诸左石崔吉龚程嵇邢滑裴陆荣"
             "叶龙车牛齐闫冉丛边白史伍华曲吕纪芦谷麦连严苏尚国官屈孟荀相柏柳哈钮"
             "段侯饶娄贺骆班敖袁聂晋桂索党翁浦涂容诸曹戚常符康寇宿隋覃焦储童曾温"
             "游靳赖雷简詹鲍廉裔窦蔡蔺廖谭缪滕颜潘黎薛霍穆戴魏瞿")
_NAME_STOP = set("的了是我你他她它们这那有在不在与和就都也而说想看去来走要会被把对到上从为")
# 组织/身份通名（宗门/弟子…）——出现高频但绝非人名，过滤（否则地名尾巴混入人物卡）
_ORG_STOP = ("宗门", "宗派", "门派", "门人", "门内", "门外", "弟子", "长老", "掌门",
             "师尊", "宗主", "师兄", "师姐", "师弟", "师妹", "道友", "前辈",
             "同门", "众人", "天下", "世间", "王朝", "朝廷", "皇朝")

# 允许 Markdown 标题前缀（docx 转出的 md 是 `# 第 1 章 …`，M3o 互转链路）
_CHAPTER_RE = re.compile(
    r"^\s*(?:#{1,6}\s+)?(?:第\s*[0-9一二三四五六七八九十百千零〇]+\s*[章回节卷]\s*\S{0,24}|"
    r"Chapter\s+\d+[\s:：]?\S{0,24})\s*$"
)
# 状态词后接时长（闭关三月后出关）或时长后接状态词（三日后闭关）两种语序；
# `[^。！？\n]` 限制状态词与时长之间不跨句（否则「渡劫九年后失踪。沉睡百日」会串行）
_TIME_PENDING_RE = re.compile(
    r"(?:(?:闭关|闭死关|入定|沉睡|疗伤|渡劫|失踪|被囚|囚禁)[^。！？\n]{0,10}?"
    r"([0-9一二三四五六七八九十百千两]+)\s*(日|天|月|年|载)(?:后|之后|以后)?"
    r"|([0-9一二三四五六七八九十百千两]+)\s*(日|天|月|年|载)(?:后|之后|以后)?"
    r"\s*(?:闭关|闭死关|入定|沉睡|疗伤|渡劫|失踪|被囚|囚禁))"
)
# 专名前缀常见动词/虚词（有/是/在…），匹配后截掉
_PREFIX_STOP = set("有是在上下里内到被把与和这那对向从为以之的又还有可会很能就都而于其")
# 动作动词前缀（服用洗髓丹/炼化法宝…），截掉后才算专名
_ACTION_VERBS = ("服用", "吞服", "服下", "吞下", "炼化", "炼成", "炼出", "炼制",
                 "手持", "祭出", "掏出", "亮出", "摸出", "拿出", "拿出", "打出",
                 "使出", "获得", "夺得", "得到", "拿起", "交予", "递出", "抛出",
                 "甩出", "扔出", "挥出", "施展", "运转", "修炼", "练成")

# 抽取协议（LLM 每片一次调用）
_EXTRACT_SYSTEM = """你是网文信息抽取员。从正文片段抽取设定要素，供构建引擎建档。
只输出 JSON，不要任何解释或前后缀。"""


# ============================================================ 1. 摄入与切片


@dataclass
class ChapterSlice:
    """切章结果（未落盘）。"""

    title: str
    text: str
    chars: int = 0
    src: str = ""  # 来源文件（用于 report）

    def __post_init__(self) -> None:
        if not self.chars:
            self.chars = len(self.text)


def _split_by_headers(text: str) -> list[tuple[str, str]]:
    """按 `第X章`/`Chapter N` 标题行切章。无标题则返回 [(标题, 全文)]。"""
    lines = text.splitlines()
    chapters: list[tuple[str, str]] = []
    cur_title = ""
    cur: list[str] = []
    for line in lines:
        if _CHAPTER_RE.match(line):
            if cur_title and cur:
                chapters.append((cur_title, "\n".join(cur).strip()))
            cur_title = line.lstrip().lstrip("#").strip()
            cur = []
        else:
            cur.append(line)
    if cur_title and cur:
        chapters.append((cur_title, "\n".join(cur).strip()))
    elif not chapters and text.strip():
        chapters.append(("", text.strip()))
    return chapters


def _split_by_words(text: str, target_words: int) -> list[tuple[str, str]]:
    """按空行段落累积，达目标字数即切（识别不到标题行时的兜底）。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[tuple[str, str]] = []
    cur: list[str] = []
    acc = 0
    for p in paras:
        cur.append(p)
        acc += len(p)
        if acc >= target_words:
            chunks.append((f"第 {len(chunks) + 1} 段", "\n".join(cur)))
            cur = []
            acc = 0
    if cur:
        chunks.append((f"第 {len(chunks) + 1} 段", "\n".join(cur)))
    return chunks


DOCX_SUFFIXES = (".docx",)
TEXT_SUFFIXES = (".md", ".txt")


def _gather_files(source: str, *, recursive: bool) -> list[Path]:
    """源：单个文件 / 目录（.md/.txt/.docx，--recursive 递归）。

    .docx 不直接进切片：由 slice_chapters 前置转换为同名 .md（M3o 互转）。
    """
    src = Path(source)
    if src.is_file():
        return [src] if src.suffix.lower() in TEXT_SUFFIXES + DOCX_SUFFIXES else []
    if src.is_dir():
        pattern = "**/*" if recursive else "*"
        return sorted(p for p in src.glob(pattern)
                      if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES + DOCX_SUFFIXES)
    return []


def _convert_docx_files(files: list[Path]) -> tuple[list[Path], list[str]]:
    """把列表中的 .docx 预转换为同名 .md（写源文件同目录，图片不导出）。

    返回 (纯文本文件列表, 警告)。同名 .md 已存在时**覆盖**——docx 是源头，
    md 视为派生物；这样重跑 ingest 总是与 docx 一致。
    """
    warns: list[str] = []
    out: list[Path] = []
    converted = False
    for f in files:
        if f.suffix.lower() != ".docx":
            out.append(f)
            continue
        from novelist.core.docxconv import DocxConvError, docx_to_markdown

        md_path = f.with_suffix(".md")
        try:
            text = docx_to_markdown(f, extract_media=False)
        except DocxConvError as e:
            warns.append(f"docx 转换失败，跳过 {f.name}: {e}")
            continue
        if md_path.exists():
            warns.append(f"{md_path.name} 已存在，已被 docx 重新生成覆盖")
        md_path.write_text(text, encoding="utf-8")
        converted = True
        out.append(md_path)
    if converted:
        warns.append("docx 已转 md 并入切片（图片未导出；如需调整可删生成的 .md 后改用手稿）")
    return out, warns


def slice_chapters(source: str, *, target_words: int = DEFAULT_WORDS_PER_CHAPTER,
                   recursive: bool = False) -> tuple[list[ChapterSlice], list[str]]:
    """读文件 → 切章。返回 (切片列表, 文件警告)。支持 .docx 源（先转 md）。"""
    files = _gather_files(source, recursive=recursive)
    if not files:
        raise FileNotFoundError(
            f"ingest source 无 .md/.txt/.docx 文件: {source}")
    files, docx_warns = _convert_docx_files(files)
    warnings: list[str] = list(docx_warns)
    out: list[ChapterSlice] = []
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            warnings.append(f"跳过空文件: {f.name}")
            continue
        headers = _split_by_headers(text)
        if len(headers) == 1 and not headers[0][0]:
            # 无标题行 → 按字数兜底切
            for title, body in _split_by_words(text, target_words):
                out.append(ChapterSlice(title=title, text=body, src=f.name))
        else:
            for title, body in headers:
                out.append(ChapterSlice(title=title, text=body, src=f.name))
    return out, warnings


def preview_chapters(io: AnswerIO, chapters: list[ChapterSlice],
                     target_words: int) -> list[ChapterSlice]:
    """切章预览确认（§6.1）：回车=全部确认 / `R`=重切 / `N`=从该章起重切 / `q`=退出。

    返回确认后的切片列表；`q` 提前退出返回空列表（不写库）。
    """
    if not io.is_tty:
        return chapters
    while True:
        lines = ["切章预览（共 %d 章）：" % len(chapters), ""]
        for i, c in enumerate(chapters, 1):
            head = (c.title or c.text.strip().splitlines()[0][:24] if c.text.strip() else "") or "（空）"
            lines.append(f"  {i:>3}. {head}  —— {c.chars} 字")
        lines += ["", "回车=全部确认 | R=重切（按字数） | N=从第 N 章起重切 | q=退出"]
        io.notify("\n".join(lines))
        ans = io.ask_free("", "")
        if ans is None:
            return []
        ans = ans.strip()
        if not ans:
            return chapters
        if ans.lower() == "q":
            return []
        if ans.lower() == "r":
            # 换 target_words 重切：把全部正文合并后按字数切
            merged = "\n\n".join(c.text for c in chapters)
            re_chunks = _split_by_words(merged, target_words)
            chapters = [ChapterSlice(t, b) for t, b in re_chunks]
            continue
        m = re.fullmatch(r"(\d+)", ans)
        if m:
            n = int(m.group(1))
            if 1 <= n <= len(chapters):
                merged = "\n\n".join(c.text for c in chapters[n - 1:])
                re_chunks = _split_by_words(merged, target_words)
                chapters = chapters[: n - 1] + [ChapterSlice(t, b) for t, b in re_chunks]
                continue
        io.notify("无法识别（回车确认 / R 重切 / q 退出）")
    return chapters


# ============================================================ 2. 抽取


def extract_deterministic(text: str, lexicon: dict | None = None) -> dict:
    """纯正则/词表抽取（零 LLM，超配额/无 provider 时保底）。

    返回 {names: {name: count}, realms: [...], locations: [...], items: [...],
           pending_lines: [...], dialogue_ratio, sentence_stats}
    """
    lexicon = lexicon or {}
    # 人名启发式：2 字名（姓氏+1字）优先；3 字名（姓氏+2字）与短名同族时取**频次高者为主名**
    # （叶蓝心 ≥ 叶蓝 → 保留全名；陆沉X 后缀字 < 陆沉 → 归并），避免长短匹配重叠计数
    short = Counter(m.group(0) for m in re.finditer(rf"[{_SURNAMES}][\u4e00-\u9fa5]", text))
    long = Counter(m.group(0) for m in re.finditer(rf"[{_SURNAMES}][\u4e00-\u9fa5]{{2}}", text))
    names: Counter[str] = Counter(short)
    for name, c in long.items():
        prefix = name[:2]
        if prefix in short:
            if c >= short[prefix]:
                names.pop(prefix, None)  # 全名为主，短名并入
                names[name] += c
            # else: 短名为主，长名视为「短名+后缀字」归并（不叠加）
        else:
            names[name] += c
    # 称谓表共现：称谓前的 2 字人名提权（单字/虚词开头不算人名）
    address = set(lexicon.get("address") or [])
    if address:
        addr_alt = "|".join(sorted(address, key=len, reverse=True))
        for m in re.finditer(rf"[\u4e00-\u9fa5]{{1,3}}(?:{addr_alt})", text):
            nm = re.sub(rf"(?:{addr_alt})$", "", m.group(0))
            if len(nm) >= 2 and nm[0] not in _PREFIX_STOP:
                names[nm] += 2
    # 高频真实人名（≥2 次才保留，过滤低频噪声与组织通名）
    kept = {k: v for k, v in names.items()
            if v >= 2 and k not in _ORG_STOP and not any(k.startswith(o) for o in _ORG_STOP)}

    realms = _match_lexicon(text, lexicon.get("realms") or [])
    locations = _match_suffix(text, lexicon.get("faction_suffix") or ["宗", "门", "城", "谷", "殿"])
    items = _match_suffix(text, lexicon.get("item_suffix") or ["丹", "剑", "诀", "经", "符"])

    pending_lines: list[str] = []
    for m in _TIME_PENDING_RE.finditer(text):
        num = m.group(1) or m.group(3)
        unit = m.group(2) or m.group(4)
        days = {"日": 1, "天": 1, "月": 30, "年": 365, "载": 365}.get(unit, 1)
        n = int(num) if num.isdigit() else _cn_num(num)
        if n > 0 and n * days <= 3650:
            what = m.group(0).strip()
            pending_lines.append(f"约定：{what}｜+{n * days}日")

    return {
        "names": kept,
        "realms": realms,
        "locations": locations,
        "items": items,
        "pending_lines": pending_lines,
        "dialogue_ratio": _dialogue_ratio(text),
        "sentence_stats": _sentence_stats(text),
    }


def _cn_num(s: str) -> int:
    """中文数字 → int；遇到非数字字符即停（容忍尾巴）。"""
    table = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "百": 100, "千": 1000}
    total = 0
    cur = 0
    for ch in s:
        if ch not in table:
            break
        v = table[ch]
        if v >= 10:
            total += (cur or 1) * v
            cur = 0
        else:
            cur = cur * 10 + v
    return total + cur


def _match_lexicon(text: str, words: list[str]) -> list[str]:
    """词表直接匹配（保持出现顺序去重）。"""
    out = []
    for w in words:
        if w and w in text and w not in out:
            out.append(w)
    return out


def _match_suffix(text: str, suffixes: list[str]) -> list[str]:
    """后缀词表匹配：前缀汉字（1-4）+ 后缀 → 专名（如 落霞宗/洗髓丹）。

    前缀粘连的动词（服用洗髓丹）与虚词（上有洗髓丹）会被截掉。
    """
    pat = re.compile(rf"[\u4e00-\u9fa5]{{1,4}}(?:{'|'.join(sorted(suffixes, key=len, reverse=True))})")
    out = []
    for m in pat.finditer(text):
        w = m.group(0)
        for vb in _ACTION_VERBS:
            if w.startswith(vb):
                w = w[len(vb):]
                break
        while w and w[0] in _PREFIX_STOP:
            w = w[1:]
        if w and w not in out:
            out.append(w)
    return out


def _dialogue_ratio(text: str) -> float:
    if not text:
        return 0.0
    quoted = len(re.findall(r"[“”\"「」『』]", text))
    return min(quoted / max(len(text), 1) * 100, 100.0)


def _sentence_stats(text: str) -> dict:
    sentences = [s for s in re.split(r"[。！？!?；;]", text) if s.strip()]
    if not sentences:
        return {"mean": 0.0, "std": 0.0}
    lengths = [len(s) for s in sentences]
    mean = sum(lengths) / len(lengths)
    var = sum((x - mean) ** 2 for x in lengths) / len(lengths)
    return {"mean": round(mean, 1), "std": round(var ** 0.5, 1)}


# ---- LLM 抽取（每片一次调用）----

_EXTRACT_PROMPT = """从下面的正文片段抽取设定要素（只输出 JSON，键名严格一致）：
{{
  "characters": [{{"name": "姓名", "gender": "male|female|unknown", "realm": "境界",
                   "traits": ["性格词"], "aliases": ["别名/称谓"], "relation": "与主角关系（无则空串）"}}],
  "realms": ["出现的境界/等级，保持原文"],
  "locations": [{{"name": "地名", "kind": "宗门|城池|秘境|…"}}],
  "items": [{{"name": "物品/功法名", "kind": "丹药|功法|法宝|…"}}],
  "key_events": ["3-5 条本章关键事件，每句含动作与结果"],
  "pending": ["未来约定原文（如「闭关三月后出关」；没有则空数组）"],
  "foreshadowing": ["埋下的伏笔/悬念；没有则空数组"],
  "style_notes": "本章文风观察（节奏/对白/用词），一两句话；没有则空串"
}}
正文片段（末尾 {max_chars} 字）：
{text}"""


def _parse_extract(raw: str) -> dict:
    text = raw.strip()
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    data = json.loads(text[start: end + 1])
    return data if isinstance(data, dict) else {}


def _llm_extract(provider, text: str, max_chars: int) -> dict:
    """一次 LLM 抽取调用；失败抛异常（调用方降级确定性）。"""
    res = provider.complete(LLMRequest(
        messages=[LLMMessage(role="system", content=_EXTRACT_SYSTEM),
                  LLMMessage(role="user", content=_EXTRACT_PROMPT.format(
                      max_chars=max_chars, text=text[-max_chars:]))],
        temperature=0.3, max_tokens_out=900, response_format="json_object",
        thinking=True))  # 判断类：已有稿→蓝图结构化抽取，开思考
    if res.blocked:
        raise RuntimeError(f"审核拦截: {res.block_reason or 'unknown'}")
    if not (res.content or "").strip():
        raise RuntimeError("抽取返回空内容")
    data = _parse_extract(res.content)
    if not data:
        raise RuntimeError("抽取解析为空")
    return data


# ============================================================ 3. 归并消歧


def _stable_id(kind: str):
    """序号制 id 生成器（char:in1/loc:in1…）。

    中文名不可直接做 id（schema 限 `^char:[A-Za-z0-9_-]+$`），且无拼音依赖；
    同一次运行内同名 → 同一 id（供归并去重），跨名递增保证唯一。
    """

    seq: dict[str, int] = {}

    def _next(name: str) -> str:
        key = f"{kind}:{name}"
        idx = seq.setdefault(key, len(seq) + 1)
        return f"{kind}:in{idx}"

    return _next


def merge_characters(bp: Blueprint, extracts: list[dict], det_names: Counter,
                     *, freq_threshold: int = MAINLINE_FREQ) -> dict:
    """LLM 人物 + 确定性人名 → 蓝图 characters（别名归一合并，频次 ≥ 阈值进主线）。

    主角判定：LLM 标 relation=主角/出现频次最高者 → role=protagonist（蓝图中无主角时）。
    返回 {added, merged, protagonist}。
    """
    # name -> {gender, realm, traits, aliases, relation, freq}
    pooled: dict[str, dict] = {}
    for ex in extracts:
        for c in ex.get("characters") or []:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            name = str(c["name"]).strip()
            if not name:
                continue
            row = pooled.setdefault(name, {"aliases": [], "freq": 0})
            for k in ("gender", "realm", "relation"):
                if c.get(k) and not row.get(k):
                    row[k] = str(c[k])
            traits = [str(t).strip() for t in (c.get("traits") or []) if str(t).strip()]
            row["traits"] = list(dict.fromkeys((row.get("traits") or []) + traits))[:3]
            for a in (c.get("aliases") or []):
                a = str(a).strip()
                if a and a not in row["aliases"]:
                    row["aliases"].append(a)
    for name, freq in det_names.items():
        if name not in pooled:
            pooled[name] = {"aliases": [], "freq": 0}
        pooled[name]["freq"] += freq

    result = {"added": 0, "merged": 0, "protagonist": ""}
    cid_gen = _stable_id("char")
    freq_sorted = sorted(pooled.items(), key=lambda kv: -kv[1]["freq"])
    for name, row in freq_sorted:
        cid = cid_gen(name)
        existing = bp.find_by_id("characters", cid) or next(
            (c for c in bp.section("characters") if c.get("name") == name), None)
        if existing is None and row["freq"] < freq_threshold and row.get("gender") is None:
            continue  # 低频且无语义信息 → 不建档（避免噪声卡）
        card = {
            "id": existing["id"] if existing else cid,
            "name": name,
            "gender": row.get("gender") or "unknown",
            "role": "minor",
            "aliases": [a for a in row["aliases"] if a != name],
            "core_traits": row.get("traits") or [],
            "status": "active",
        }
        if row.get("realm"):
            card["power"] = {"level": row["realm"]}
        if existing:
            merged = dict(existing)
            for k, v in card.items():
                if k == "id":
                    continue
                if v not in (None, "", [], {}):
                    merged[k] = v
            bp.upsert("characters", merged)
            result["merged"] += 1
        else:
            bp.upsert("characters", card)
            result["added"] += 1
        bp.set_provenance(f"characters[{card['id']}].name", "ingested", 0.8,
                          evidence="已有正文")
        # 主角：LLM 明示 relation=主角，或（无明示时）最高频
        if not result["protagonist"]:
            is_proto = (row.get("relation") == "主角"
                        or (row.get("relation") or "").startswith("主角"))
            if is_proto:
                result["protagonist"] = card["id"]
    # 无明示主角 → 最高频者
    if not result["protagonist"] and freq_sorted:
        cid = cid_gen(freq_sorted[0][0])
        found = next((c for c in bp.section("characters")
                      if c.get("id") == cid or c.get("name") == freq_sorted[0][0]), None)
        if found:
            result["protagonist"] = found["id"]
    if result["protagonist"]:
        for c in bp.section("characters"):
            if c.get("id") == result["protagonist"]:
                c["role"] = "protagonist"
    return result


# ============================================================ 4. 文风画像


def style_from_ingest(bp: Blueprint, extracts: list[dict], texts: list[str],
                      pack: dict | None = None) -> None:
    """确定性指标 + 抽取 style_notes 归纳 → 蓝图 style（provenance=ingested）。"""
    st = dict(bp.get("style") or {})
    ds = (pack or {}).get("default_style") or {}
    st.setdefault("pov", ds.get("pov", "第三人称限知（主角视角）"))
    st.setdefault("target_words_per_chapter", ds.get("target_words_per_chapter", 2400))
    notes = [str(ex.get("style_notes")) for ex in extracts if ex.get("style_notes")]
    if notes:
        st["tone"] = [notes[0][:40]]  # 归纳：取首条观察（长文自动压缩）
        bp.set_provenance("style.tone", "ingested", 0.6, evidence="已有正文文风观察")
    # 确定性指标不落 style（观察用），交给 show 报告
    bp.data["style"] = st
    bp.set_provenance("style.pov", "ingested", 0.8)
    bp.set_provenance("style.target_words_per_chapter", "template", 1.0)


# ============================================================ 5. 卷章编码 + 记忆初始化


def _encode_volumes(ws: Workspace, project_id: str, bp: Blueprint,
                    chapters: list[ChapterSlice], extracts: list[dict],
                    chapters_per_volume: int) -> dict:
    """切片 → 正式章节 + 细纲反写（done=true）+ 蓝图 chapters[]。返回 {volumes, written}。"""
    volumes: dict[int, list[int]] = {}
    written = 0
    for i, c in enumerate(chapters, 1):
        vol = (i - 1) // chapters_per_volume + 1
        ch = (i - 1) % chapters_per_volume + 1
        volumes.setdefault(vol, []).append(ch)
        # 正式章节（正文原文）
        p = ws.chapter_path(project_id, vol, ch)
        p.parent.mkdir(parents=True, exist_ok=True)
        ws.write_text(p, f"# {c.title or f'第 {ch} 章'}\n\n{c.text.strip()}\n")
        # 细纲反写（done=true）：key_events 来自抽取或占位
        ex = extracts[i - 1] if i - 1 < len(extracts) else {}
        events = [str(e) for e in (ex.get("key_events") or [])]
        if not events:
            events = [f"第 {ch} 章：{c.title or '主线推进'}"]
        gist = {
            "vol": vol, "ch": ch,
            "title": c.title or f"第 {ch} 章",
            "key_events": events[:6],
            "turns": [],
            "characters": [],
            "threads_involved": [],
            "done": True,
        }
        gist_path = ws.outline_chapter_path(project_id, vol, ch)
        gist_path.parent.mkdir(parents=True, exist_ok=True)
        ws.write_text(gist_path, _render_gist_md_done(gist))
        _upsert_chapter_done(bp, gist)
        written += 1
    return {"volumes": len(volumes), "written": written}


def _render_gist_md_done(gist: dict) -> str:
    fm = {
        "id": f"ch:{gist['vol']}:{gist['ch']}",
        "vol": gist["vol"], "ch": gist["ch"],
        "title": gist["title"],
        "key_events": gist["key_events"],
        "done": True,
    }
    return ("---\n" + json.dumps(fm, ensure_ascii=False) + "\n---\n\n"
            f"# 第 {gist['ch']} 章 {gist['title']}\n\n"
            f"key_events: {json.dumps(gist['key_events'], ensure_ascii=False)}\n\n"
            "（已有正文，细纲仅索引）\n")


def _upsert_chapter_done(bp: Blueprint, gist: dict) -> None:
    chapters = bp.section("chapters")
    for i, x in enumerate(chapters):
        if x.get("vol") == gist["vol"] and x.get("ch") == gist["ch"]:
            chapters[i] = gist
            return
    chapters.append(gist)


def init_memory(ws: Workspace, project_id: str, chapters: list[ChapterSlice],
                provider, *, chapters_per_volume: int,
                embedding=None, ingest_max_calls: int,
                calls_used: int, max_chars: int = 2500) -> tuple[int, list[str]]:
    """chronicler 逐章抽事件写 memory/ + MemoryIndex.rebuild。

    返回 (调用数增量, 告警)。LLM 为 None 或配额耗尽 → 跳过（不静默，记 warning）。
    """
    if provider is None:
        return 0, ["记忆初始化跳过：无 LLM provider（无法抽取事件）"]
    from ..core.chronicler import Chronicler
    from ..core.memory import MemoryIndex

    chronicler = Chronicler(ws, project_id, llm=provider, embedding=embedding)
    warnings: list[str] = []
    used = 0
    remaining = ingest_max_calls - calls_used
    if remaining <= 0:
        return 0, ["记忆初始化跳过：抽取配额已耗尽"]
    for i, c in enumerate(chapters, 1):
        if used >= remaining:
            warnings.append(f"记忆初始化：第 {i} 章起跳过（抽取配额 {ingest_max_calls} 耗尽）")
            break
        vol = (i - 1) // chapters_per_volume + 1
        ch = (i - 1) % chapters_per_volume + 1
        try:
            rep = chronicler.run(c.text[:max_chars], vol, ch, tag=f"ingest{i}")
            used += 1
            if rep.warnings:
                warnings.extend(f"记忆初始化 第 {vol}-{ch} 章: {w}" for w in rep.warnings[:2])
        except Exception as e:  # noqa: BLE001 - 单章失败不阻断记忆初始化
            used += 1
            warnings.append(f"记忆初始化 第 {vol}-{ch} 章失败: {e}")
    MemoryIndex.load(ws, project_id).rebuild(ws, project_id, embedding=embedding)
    return used, warnings


def warmup_entities(ws: Workspace, project_id: str, chapters: list[ChapterSlice],
                    chapters_per_volume: int) -> dict:
    """EntityTracker 逐章 update_from_chapter（纯正则）重建 entity_progress（warm-up）。"""
    from ..core.entity import EntityTracker

    t = EntityTracker.load(ws, project_id)
    stats = {"new": 0, "stage_up": 0, "mentioned": 0}
    for i, c in enumerate(chapters, 1):
        vol = (i - 1) // chapters_per_volume + 1
        ch = (i - 1) % chapters_per_volume + 1
        r = t.update_from_chapter(c.text, vol, ch)
        stats["new"] += len(r["new"])
        stats["stage_up"] += len(r["stage_up"])
    t.save()
    stats["mentioned"] = len([e for e in t.entities.values() if e.mentions > 0])
    return stats


# ============================================================ 6. 主入口


@dataclass
class IngestResult:
    ok: bool
    project_id: str
    chapters_ingested: int = 0
    volumes_encoded: int = 0
    calls_used: int = 0
    characters_found: int = 0
    downgraded: list[int] = field(default_factory=list)  # 降级为纯确定性抽取的章号
    warnings: list[str] = field(default_factory=list)
    blueprint_path: str = ""
    quit_early: bool = False  # 切章确认 q 退出（未写库）


def _genres_pack(genre: str | None) -> tuple[dict, str]:
    if genre:
        try:
            pack = _genres.load_pack_for(genre)
            return pack, pack.get("id") or genre
        except KeyError:
            pass
    return _genres.load_pack(_genres.GENERIC_ID), _genres.GENERIC_ID


def run_ingest(ws: Workspace, project_id: str, source: str, *,
               provider=None, embedding=None,
               genre: str | None = None,
               chapters_per_volume: int = DEFAULT_CHAPTERS_PER_VOLUME,
               target_words: int = DEFAULT_WORDS_PER_CHAPTER,
               ingest_max_calls: int = DEFAULT_INGEST_MAX_CALLS,
               mode: str = "auto", dry_run: bool = False,
               recursive: bool = False,
               io: AnswerIO | None = None,
               max_chars: int = 2500) -> IngestResult:
    """已有稿子 → 蓝图 + 正式章节 + 记忆初始化（docs/10 §6）。

    `dry_run=True`：只切章预览，不写库（CLI --dry-run）。
    """
    from .io_console import ConsoleIO

    io = io or ConsoleIO()
    warnings: list[str] = []
    state = ForgeState.load(ws, project_id)
    state.mode = "ingest"
    state.interaction = mode
    state.stage = "ingest"
    state.started_at = state.started_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state.save(ws, project_id)
    append_transcript(ws, project_id, "ingest.start", source=source[:200],
                      chapters_per_volume=chapters_per_volume,
                      ingest_max_calls=ingest_max_calls)

    # 1) 切片 + 预览确认
    try:
        chapters, slice_warns = slice_chapters(source, target_words=target_words,
                                               recursive=recursive)
    except FileNotFoundError as e:
        raise ValueError(str(e)) from e
    warnings += slice_warns
    if not chapters:
        return IngestResult(ok=False, project_id=project_id, warnings=warnings)
    append_transcript(ws, project_id, "ingest.slice", count=len(chapters),
                      total_chars=sum(c.chars for c in chapters))
    confirmed = preview_chapters(io, chapters, target_words)
    if not confirmed:
        return IngestResult(ok=False, project_id=project_id, warnings=warnings,
                            quit_early=True)
    chapters = confirmed
    if dry_run:
        return IngestResult(ok=True, project_id=project_id, chapters_ingested=len(chapters),
                            warnings=warnings + [f"dry-run：切片 {len(chapters)} 章，未写库"])

    # 2) 抽取（确定性优先；LLM 补语义，独立配额）
    pack, pack_id = _genres_pack(genre)
    lexicon = pack.get("extract_lexicon") or {}
    extracts: list[dict] = []
    downgraded: list[int] = []
    calls_used = 0
    for i, c in enumerate(chapters, 1):
        det = extract_deterministic(c.text, lexicon)
        if provider is not None and calls_used < ingest_max_calls:
            try:
                extracts.append(_llm_extract(provider, c.text, max_chars))
                calls_used += 1
            except Exception as e:  # noqa: BLE001 - 单章抽取失败降级确定性（docs/10 §12）
                extracts.append({})
                calls_used += 1
                warnings.append(f"第 {i} 章 LLM 抽取失败，降级确定性: {e}")
                downgraded.append(i)
        else:
            extracts.append({})
            if provider is not None:
                downgraded.append(i)
                if i == len(chapters) or calls_used >= ingest_max_calls:
                    warnings.append(f"抽取配额 {ingest_max_calls} 耗尽：第 {i} 章起降级纯确定性")
    if provider is None and chapters:
        downgraded = list(range(1, len(chapters) + 1))
        warnings.append(f"无 LLM provider：{len(chapters)} 章全部降级纯确定性抽取")
    append_transcript(ws, project_id, "ingest.extract", calls_used=calls_used,
                      downgraded=[i for i in downgraded])

    # 3) 蓝图初始化 + 归并消歧 + 文风画像
    bp = Blueprint.blank()
    meta = bp.data["meta"]
    meta["title"] = project_id
    meta["genre"] = pack.get("genre") or pack_id
    meta["template"] = pack_id
    meta["logline"] = ""
    meta["scale"] = {
        "volumes": max(1, (len(chapters) + chapters_per_volume - 1) // chapters_per_volume),
        "chapters_per_volume": chapters_per_volume,
        "target_words_per_chapter": target_words,
    }
    bp.set_provenance("meta.title", "ingested", 1.0)
    bp.set_provenance("meta.genre", "ingested", 0.9)
    bp.set_provenance("meta.template", "ingested", 1.0)
    bp.set_provenance("meta.scale", "ingested", 1.0)
    wv = bp.data["worldview"]
    wv["power_system"] = {"mechanic": (pack.get("power_system") or {}).get("mechanic", ""),
                          "levels": list((pack.get("power_system") or {}).get("levels") or [])}
    det_names: Counter = Counter()
    for det in [extract_deterministic(c.text, lexicon) for c in chapters]:
        det_names.update(det["names"])
    # 世界要素（确定性优先，LLM 补充）
    for ex in extracts:
        for r in ex.get("realms") or []:
            if str(r) and str(r) not in wv["power_system"]["levels"]:
                wv["power_system"]["levels"].append(str(r))
    loc_gen = _stable_id("loc")
    item_gen = _stable_id("item")
    for ex in extracts:
        for loc in ex.get("locations") or []:
            if isinstance(loc, dict) and loc.get("name"):
                bp.upsert("locations", {"id": loc_gen(str(loc["name"])),
                                        "name": str(loc["name"]),
                                        "category": str(loc.get("kind") or "")})
        for it in ex.get("items") or []:
            if isinstance(it, dict) and it.get("name"):
                bp.upsert("items", {"id": item_gen(str(it["name"])),
                                    "name": str(it["name"]),
                                    "category": str(it.get("kind") or "")})
    merge = merge_characters(bp, extracts, det_names)
    style_from_ingest(bp, extracts, [c.text for c in chapters], pack)
    bp.save(ws, project_id)
    append_transcript(ws, project_id, "ingest.blueprint",
                      characters=merge["added"] + merge["merged"],
                      protagonist=merge["protagonist"])

    # 4) 卷章编码 + 细纲反写（done=true）
    enc = _encode_volumes(ws, project_id, bp, chapters, extracts, chapters_per_volume)
    bp.save(ws, project_id)
    append_transcript(ws, project_id, "ingest.encode", **enc)

    # 5) 记忆初始化 + 实体 warm-up（先 sync_bible 供 chronicler/EntityTracker 读）
    from .nodes import sync_bible, synthesize_worldstate

    sync_bible(ws, project_id, bp)
    mem_used, mem_warns = init_memory(ws, project_id, chapters, provider,
                                      chapters_per_volume=chapters_per_volume,
                                      embedding=embedding,
                                      ingest_max_calls=ingest_max_calls,
                                      calls_used=calls_used, max_chars=max_chars)
    calls_used += mem_used
    warnings += mem_warns
    warmup_entities(ws, project_id, chapters, chapters_per_volume)
    # worldstate：Chronicler 逐章推进的 time/pending 是事实源（「时间：/约定：」行，
    # 约定 due 以各章当时 day 锚定——ADR-019「预计完成时间」语义，用户 2026-09-01 拍板）。
    # 本步只把蓝图合成的人物初始态合并进去，**不覆盖时间轴**
    # （曾用 synthesize_worldstate 无条件覆盖 now=0，抹掉逐章推进结果——修复点）。
    # 确定性 pending_lines 仅作无 LLM 兜底（fake/降级链路 Chronicler 抽不出「约定：」行）。
    from ..core.timeline import now_of, parse_pending_line

    synth = synthesize_worldstate(bp)
    ws_path = ws.bible_path(project_id, "worldstate")
    current = ws.read_json(project_id, ws_path, required=False)
    if not isinstance(current, dict):
        current = {}
    cur_time = current.get("time")
    ws_data: dict = {
        "time": cur_time if isinstance(cur_time, dict) and "now" in cur_time else synth["time"],
        "pending": list(current.get("pending") or []),
        "characters": synth["characters"],
    }
    # Chronicler 给人物打的不可出场期/状态历史保留（synthesize 只合初始态）
    for cid, cur in (current.get("characters") or {}).items():
        entry = ws_data["characters"].get(cid)
        if isinstance(entry, dict) and isinstance(cur, dict):
            for k in ("unavailable_until", "unavailable_since", "unavailable_reason", "history"):
                if cur.get(k):
                    entry[k] = cur[k]
    if not ws_data["pending"]:
        # 兜底：due = 当前 day + 持续时间（「预计完成时间」语义；无 LLM 时 now 通常为 0）
        pending_lines_all: list[str] = []
        for c in chapters:
            pending_lines_all.extend(extract_deterministic(c.text, lexicon)["pending_lines"])
        base = now_of(ws_data)
        pending_items = []
        for i, line in enumerate(dict.fromkeys(pending_lines_all), 1):
            parsed = parse_pending_line(line)
            if parsed:
                what, dt, _warn = parsed
                # 字段与 timeline.add_pending 对齐；status 必须是 schema 枚举 "scheduled"
                pending_items.append({"id": f"pd:ingest{i}", "who": "", "what": what,
                                      "due": base + int(dt), "span": max(int(dt), 1),
                                      "created_t": base, "status": "scheduled",
                                      "created_at": {"vol": 1, "ch": 0},
                                      "overdue": 0, "block_count": 0})
        ws_data["pending"] = pending_items
    ws.write_json(ws_path, ws_data)
    bp.save(ws, project_id)

    # 6) 缺口检测 → 回落商讨（interactive 且 TTY 才真问）
    consult: ConsultResult | None = None
    if mode == "interactive":
        if provider is None:
            warnings.append("interactive 回落商讨跳过：无 LLM provider（候选生成不可用）")
        else:
            consult = run_consult(ws, project_id, bp, provider=provider,
                                  io=io, slots=slots_for_genre(pack))
            warnings += consult.warnings
    if consult is None or not consult.quit_early:
        state.touch_stage(ws, project_id, "ingested")

    state.calls_used = calls_used
    state.save(ws, project_id)
    append_transcript(ws, project_id, "ingest.end",
                      chapters=len(chapters), calls_used=calls_used,
                      downgraded=downgraded, quit_early=bool(consult and consult.quit_early))
    return IngestResult(
        ok=True,
        project_id=project_id,
        chapters_ingested=len(chapters),
        volumes_encoded=enc["volumes"],
        calls_used=calls_used,
        characters_found=merge["added"] + merge["merged"],
        downgraded=downgraded,
        warnings=warnings,
        blueprint_path=str(ws._abs(f"{project_id}/workspace/forge/blueprint.json")),  # noqa: SLF001
        quit_early=bool(consult and consult.quit_early),
    )
