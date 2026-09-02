"""Markdown ⇄ Word(.docx) 互转（M3o，零第三方依赖）。

项目依赖刻意精简（docs/08 依赖表只有 click/pydantic/fastapi/jsonschema…），
因此不引入 python-docx：直接用 stdlib `zipfile` 读写 OOXML 包。docx 本质是一个
zip，正文在 `word/document.xml`，样式在 `word/styles.xml`。

对外 API：

- `docx_to_markdown(src, *, media_dir=..., extract_media=True) -> str`
- `markdown_to_docx(md, dst, *, title=..., font_body=..., font_heading=...,
                    indent=True, chapter_page_break=True, title_style=True) -> Path`
- `convert(src, dst, **kw) -> Path`：按后缀自动分派方向

设计取舍（为什么不用库 / 为什么有些地方不"原生"）：

1. **列表用文本前缀 + 缩进，不生成 numbering.xml**。原生编号需要在包里额外维护
   `numbering.xml` 与 numId/abstractNumId 两级映射，收益（Word 里可继续自动编号）
   小于成本（实现 + 回归面）。视觉结果一致，且转换产物以"给人看/给出版社"为主。
2. **表格原生生成**（`w:tbl`）。表格若退化成等宽文本会彻底不可读，值得做真表格。
3. **读侧只转义行首标记字符**，行内的 `*`/`_` 不转义——转出的 md 要喂回 ingest
   进正文与记忆层，反斜杠会污染语料（"他\\_说"）。
4. **写侧剔除 XML 非法控制字符**。OOXML 不接受 C0 控制符（除 \\t\\r\\n），
   模型生成的偶发 0x0B/0x0C 会让 Word 报"文件损坏"，这是转换类代码最常见的坑。
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

__all__ = [
    "docx_to_markdown",
    "markdown_to_docx",
    "convert",
    "is_docx",
    "DocxConvError",
]

# ---------------------------------------------------------------- 命名空间

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PR = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_CP = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
_DC = "http://purl.org/dc/elements/1.1/"
_DCTERMS = "http://purl.org/dc/terms/"
_EP = ("http://schemas.openxmlformats.org/officeDocument/2006/"
       "extended-properties")

# 关系类型
_RT_DOC = f"{_PR}/officeDocument"
_RT_CORE = "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties"
_RT_EXT = f"{_PR}/extended-properties"
_RT_STYLES = f"{_PR}/styles"
_RT_IMAGE = f"{_PR}/image"
_RT_HYPERLINK = f"{_PR}/hyperlink"

# document.xml.rels 中 rId1 固定给 styles.xml，外部链接从 2 开始编号
_FIRST_LINK_ID = 2


class DocxConvError(ValueError):
    """转换失败（非 docx 包 / 正文缺失 / 非法输入）。"""


def _qn(tag: str, ns: str = _W) -> str:
    return f"{{{ns}}}{tag}"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _esc(s: str) -> str:
    """XML 转义（属性值与文本节点通用）。"""
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


_CTRL_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")


def _xml_safe(s: str) -> str:
    """剔除 XML 1.0 非法控制字符（OOXML 不接受，会致 Word 报文件损坏）。"""
    return _CTRL_RE.sub("", s)


def is_docx(path: str | Path) -> bool:
    """按 zip 内部结构判断（不信扩展名——用户常把 .docx 命名成 .doc）。"""
    p = Path(path)
    if not p.is_file():
        return False
    try:
        with zipfile.ZipFile(p) as z:
            names = set(z.namelist())
        return "word/document.xml" in names
    except (zipfile.BadZipFile, OSError):
        return False


# ==================================================================== 读侧

@dataclass
class _StyleInfo:
    name: str = ""
    outline: int | None = None   # 0-8，对应 Heading1-9
    based_on: str = ""


@dataclass
class _ReadCtx:
    styles: dict[str, _StyleInfo] = field(default_factory=dict)
    rels: dict[str, tuple[str, str]] = field(default_factory=dict)   # rId → (Type, Target)
    numbering: dict[str, str] = field(default_factory=dict)          # numId → numFmt
    media: dict[str, str] = field(default_factory=dict)              # rId → 输出相对路径


_HEADING_RE = re.compile(r"(?:heading|标题|title)\s*[-_ ]?(\d+)", re.I)
_TITLE_ONLY_RE = re.compile(r"^(?:title|标题|书名)$", re.I)


def _parse_styles(z: zipfile.ZipFile) -> dict[str, _StyleInfo]:
    """styleId → (名字 / 大纲级别 / 继承)。标题识别全靠它。"""
    out: dict[str, _StyleInfo] = {}
    try:
        data = z.read("word/styles.xml")
    except KeyError:
        return out
    root = ET.fromstring(data)
    for st in root.findall(_qn("style")):
        sid = st.get(_qn("styleId")) or ""
        if not sid:
            continue
        info = _StyleInfo()
        nm = st.find(_qn("name"))
        if nm is not None:
            info.name = nm.get(_qn("val")) or ""
        ppr = st.find(_qn("pPr"))
        if ppr is not None:
            obl = ppr.find(_qn("outlineLvl"))
            if obl is not None and (obl.get(_qn("val")) or "").isdigit():
                info.outline = int(obl.get(_qn("val")))
            base = ppr.find(_qn("basedOn"))
            if base is not None:
                info.based_on = base.get(_qn("val")) or ""
        out[sid] = info
    return out


def _parse_rels(z: zipfile.ZipFile, part: str) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    try:
        data = z.read(part)
    except KeyError:
        return out
    for rel in ET.fromstring(data):
        rid = rel.get("Id")
        if rid:
            out[rid] = (rel.get("Type") or "", rel.get("Target") or "")
    return out


def _parse_numbering(z: zipfile.ZipFile) -> dict[str, str]:
    """numId → numFmt（bullet / decimal…）。只取第一级，够判定有序无序。"""
    out: dict[str, str] = {}
    try:
        data = z.read("word/numbering.xml")
    except KeyError:
        return out
    root = ET.fromstring(data)
    abstract: dict[str, str] = {}
    for ab in root.findall(_qn("abstractNum")):
        aid = ab.get(_qn("abstractNumId"))
        lvl = ab.find(f"{_qn('lvl')}[@{{{_W}}}ilvl='0']")
        fmt = ""
        if lvl is not None:
            nf = lvl.find(_qn("numFmt"))
            if nf is not None:
                fmt = nf.get(_qn("val")) or ""
        if aid:
            abstract[aid] = fmt
    for num in root.findall(_qn("num")):
        nid = num.get(_qn("numId"))
        ref = num.find(_qn("abstractNumId"))
        if nid and ref is not None:
            out[nid] = abstract.get(ref.get(_qn("val")) or "", "")
    return out


def _style_heading_level(ctx: _ReadCtx, style_id: str) -> int | None:
    """样式 → 标题级别（1-6）。三级判定：名字 → 自身 outlineLvl → 继承链。"""
    seen: set[str] = set()
    cur = style_id
    while cur and cur not in seen:
        seen.add(cur)
        info = ctx.styles.get(cur)
        if info is None:
            return None
        if info.outline is not None:
            return min(max(info.outline + 1, 1), 6)
        m = _HEADING_RE.search(info.name or "")
        if m:
            return min(max(int(m.group(1)), 1), 6)
        if _TITLE_ONLY_RE.match(info.name or ""):
            return 1
        cur = info.based_on
    return None


def _style_name(ctx: _ReadCtx, style_id: str) -> str:
    info = ctx.styles.get(style_id)
    return (info.name if info else "") or ""


def _para_style(p: ET.Element) -> str:
    ppr = p.find(_qn("pPr"))
    if ppr is None:
        return ""
    ps = ppr.find(_qn("pStyle"))
    return (ps.get(_qn("val")) or "") if ps is not None else ""


def _para_heading_level(p: ET.Element, ctx: _ReadCtx) -> int | None:
    ppr = p.find(_qn("pPr"))
    if ppr is None:
        return None
    ps = ppr.find(_qn("pStyle"))
    if ps is not None:
        lvl = _style_heading_level(ctx, ps.get(_qn("val")) or "")
        if lvl:
            return lvl
    obl = ppr.find(_qn("outlineLvl"))
    if obl is not None and (obl.get(_qn("val")) or "").isdigit():
        return min(max(int(obl.get(_qn("val"))) + 1, 1), 6)
    return None


def _is_bold(rpr: ET.Element | None) -> bool:
    if rpr is None:
        return False
    for tag in ("b", "bCs"):
        el = rpr.find(_qn(tag))
        if el is not None and (el.get(_qn("val")) or "true").lower() not in ("0", "false", "off"):
            return True
    return False


def _is_italic(rpr: ET.Element | None) -> bool:
    if rpr is None:
        return False
    for tag in ("i", "iCs"):
        el = rpr.find(_qn(tag))
        if el is not None and (el.get(_qn("val")) or "true").lower() not in ("0", "false", "off"):
            return True
    return False


def _is_code(rpr: ET.Element | None) -> bool:
    """行内代码：识别本工具写出的 CodeChar 字符样式（读侧重加反引号）。"""
    if rpr is None:
        return False
    rs = rpr.find(_qn("rStyle"))
    return rs is not None and (rs.get(_qn("val")) or "") == "CodeChar"


def _container_md(el: ET.Element, ctx: _ReadCtx, media_dir: Path | None) -> str:
    """一个容器（段落 / hyperlink / smartTag）内的全部内容 → md 片段。"""
    parts: list[str] = []
    for child in el:
        tag = _local(child.tag)
        if tag == "r":
            rpr = child.find(_qn("rPr"))
            bold, italic = _is_bold(rpr), _is_italic(rpr)
            code = _is_code(rpr)
            buf: list[str] = []
            for sub in child:
                st = _local(sub.tag)
                if st == "t":
                    buf.append(sub.text or "")
                elif st == "tab":
                    buf.append(" ")
                elif st in ("br", "cr"):
                    buf.append("\n")
                elif st == "drawing":
                    alt = _image_md(sub, ctx, media_dir)
                    if alt:
                        buf.append(alt)
            text = "".join(buf)
            if text:
                if code:
                    text = f"`{text}`"
                elif italic and not bold:
                    text = f"*{text}*"
                elif bold:
                    text = f"**{text}**"
                parts.append(text)
        elif tag == "hyperlink":
            rid = child.get(_qn("id", _R)) or ""
            target = ctx.rels.get(rid, ("", ""))[1]
            inner = _container_md(child, ctx, media_dir).strip()
            if inner:
                parts.append(f"[{inner}]({target})" if target else inner)
        elif tag in ("smartTag", "ins", "sdtContent", "dir", "bdo"):
            parts.append(_container_md(child, ctx, media_dir))
        elif tag == "tbl":
            parts.append(_table_md(child, ctx, media_dir))
    return "".join(parts)


def _image_md(drawing: ET.Element, ctx: _ReadCtx, media_dir: Path | None) -> str:
    """drawing → md 图片引用（可选把媒体文件抽到 media_dir）。"""
    blip = None
    for el in drawing.iter():
        if _local(el.tag) == "blip":
            blip = el
            break
    if blip is None:
        return ""
    rid = blip.get(_qn("embed", _R)) or ""
    if not rid or rid not in ctx.media:
        return ""
    return f"![图片]({ctx.media[rid]})"


def _table_md(tbl: ET.Element, ctx: _ReadCtx, media_dir: Path | None) -> str:
    """表格 → md 表格（首行作表头，仅当行数 ≥ 2）。"""
    rows: list[list[str]] = []
    for tr in tbl.findall(_qn("tr")):
        cells: list[str] = []
        for tc in tr.findall(_qn("tc")):
            segs = []
            for p in tc.findall(_qn("p")):
                t = _container_md(p, ctx, media_dir).strip()
                if t:
                    segs.append(t)
            cells.append("<br>".join(segs).replace("|", "\\|"))
        rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |",
             "|" + "|".join(["---"] * width) + "|"]
    for r in rows[1:]:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


# 行首标记字符：转 md 时转义，避免"1. 他起身"被当成有序列表。
_LEAD_ESC_RE = re.compile(r"^(\s*)([#>\-+*=]|(\d+)\.)(\s|$)")


def _escape_leading(text: str) -> str:
    """行首标记字符转义，避免「1. 他起身」被读回成有序列表。

    有序列表要转义的是点号而不是数字（`1\\. 他起身`）—— 在数字前加反斜杠
    得到 `\\1. 他起身`，md 解析器照样当列表。
    """
    m = _LEAD_ESC_RE.match(text)
    if not m:
        return text
    if m.group(3):                       # 有序列表：数字 + 点
        return f"{m.group(1)}{m.group(3)}\\." + text[m.end(2):]
    return f"{m.group(1)}\\{m.group(2)}" + text[m.end(2):]


def _para_md(p: ET.Element, ctx: _ReadCtx, media_dir: Path | None) -> str:
    ppr = p.find(_qn("pPr"))
    text = _container_md(p, ctx, media_dir)
    # 段首缩进（空格/tab）在 md 里无意义，去掉；段内换行保留
    text = "\n".join(line.strip() for line in text.split("\n"))
    if not text.strip():
        return ""

    lvl = _para_heading_level(p, ctx)
    if lvl:
        return f"{'#' * lvl} {text.strip()}"

    # 引用：Quote 样式 → `> ` 前缀（与写侧对称，往返一致）
    sid = _para_style(p)
    if sid and (_style_name(ctx, sid).strip().lower() == "quote"
                or sid.lower() == "quote"):
        return "> " + text.strip().replace("\n", "\n> ")

    prefix = ""
    if ppr is not None:
        npr = ppr.find(_qn("numPr"))
        if npr is not None:
            nid = npr.find(_qn("numId"))
            ilvl = npr.find(_qn("ilvl"))
            depth = int(ilvl.get(_qn("val")) or 0) if (
                ilvl is not None and (ilvl.get(_qn("val")) or "").isdigit()) else 0
            fmt = ctx.numbering.get(nid.get(_qn("val")) or "", "") if nid is not None else ""
            if fmt == "bullet" or not fmt:
                prefix = "  " * depth + "- "
            else:
                prefix = "  " * depth + "1. "
    body = text.strip() if prefix else _escape_leading(text.strip())
    return (prefix + body) if prefix else body


def _extract_media(z: zipfile.ZipFile, out_dir: Path,
                   wanted: set[str]) -> dict[str, str]:
    """把用到的图片写到 out_dir，返回 rId → 相对路径（md 里引用）。"""
    out: dict[str, str] = {}
    try:
        rels = _parse_rels(z, "word/_rels/document.xml.rels")
    except Exception:  # noqa: BLE001 - 任何解析异常都退化为"不抽图"
        return out
    names = set(z.namelist())
    for rid, (rtype, target) in rels.items():
        if rtype != _RT_IMAGE or rid not in wanted:
            continue
        # 关系 Target 相对 word/ 部件；也可能是 /word/... 绝对路径
        rel = target.lstrip("/")
        member = next((c for c in (rel, f"word/{rel}") if c in names), "")
        if not member:
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        fname = Path(member).name or f"{rid}.bin"
        (out_dir / fname).write_bytes(z.read(member))
        out[rid] = f"{out_dir.name}/{fname}"
    return out


def docx_to_markdown(src: str | Path, *, media_dir: str | Path | None = None,
                     extract_media: bool = True) -> str:
    """Word(.docx) → Markdown 文本。

    - 标题：优先 `w:outlineLvl` / 样式名（Heading N / 标题 N），识别不到按正文
    - 粗体/斜体/超链接保留；表格转 md 表格；列表按 numbering 判定有序无序
    - 图片：默认抽出到 `<md 同名>_media/`，md 里写相对引用；`extract_media=False` 丢弃
    """
    p = Path(src)
    if not is_docx(p):
        raise DocxConvError(f"不是有效的 .docx 包（或文件不存在）: {p}")

    with zipfile.ZipFile(p) as z:
        ctx = _ReadCtx(styles=_parse_styles(z),
                       rels=_parse_rels(z, "word/_rels/document.xml.rels"),
                       numbering=_parse_numbering(z))
        try:
            root = ET.fromstring(z.read("word/document.xml"))
        except KeyError as e:  # pragma: no cover - is_docx 已校验
            raise DocxConvError(f"docx 缺 word/document.xml: {p}") from e
        body = root.find(_qn("body"))
        if body is None:  # pragma: no cover
            raise DocxConvError(f"docx 缺 w:body: {p}")

        wanted: set[str] = set()
        for el in body.iter():
            if _local(el.tag) == "blip":
                rid = el.get(_qn("embed", _R))
                if rid:
                    wanted.add(rid)
        mdir: Path | None = None
        if extract_media and wanted:
            mdir = Path(media_dir) if media_dir else p.with_name(p.stem + "_media")
            ctx.media = _extract_media(z, mdir, wanted)

        blocks: list[str] = []
        for el in body:
            tag = _local(el.tag)
            if tag == "p":
                blocks.append(_para_md(el, ctx, mdir))
            elif tag == "tbl":
                blocks.append(_table_md(el, ctx, mdir))
            elif tag == "sdt":
                inner = el.find(_qn("sdtContent"))
                if inner is not None:
                    for sub in inner:
                        st = _local(sub.tag)
                        if st == "p":
                            blocks.append(_para_md(sub, ctx, mdir))
                        elif st == "tbl":
                            blocks.append(_table_md(sub, ctx, mdir))
            elif tag == "sectPr":
                continue
        # 表格本身是多行文本，需与前后段落隔开
        text = "\n\n".join(b for b in blocks if b != "")
    return _CTRL_RE.sub("", text).strip() + "\n"


# ==================================================================== 写侧

@dataclass
class _Span:
    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False
    strike: bool = False
    href: str = ""


@dataclass
class _Block:
    kind: str                       # title|heading|para|quote|ul|ol|code|table|hr
    level: int = 0
    text: str = ""
    spans: list[_Span] = field(default_factory=list)
    items: list[list[_Span]] = field(default_factory=list)
    rows: list[list[list[_Span]]] = field(default_factory=list)
    lang: str = ""


# 顺序敏感：code 必须先于 em/strong，否则 `a*b*c` 会被斜体吃掉
_INLINE_RE = re.compile(
    r"(?P<esc>\\[\\`*_{}\[\]()#+\-.!>~|])"
    r"|(?P<code>`(?P<code_t>[^`]+?)`)"
    r"|(?P<del>~~(?P<del_t>.+?)~~)"
    r"|(?P<strong>\*\*(?P<strong_t>.+?)\*\*|__(?P<strong_u>.+?)__)"
    r"|(?P<em>\*(?P<em_t>[^*\n]+?)\*|(?<![\w\\])_(?P<em_u>[^_\n]+?)_(?![\w]))"
    r"|(?P<link>\[(?P<link_t>[^\]]*)\]\((?P<link_u>[^)\s]*)(?:\s+\"[^\"]*\")?\))"
)


def _split_inline(text: str) -> list[_Span]:
    """行内标记 → span 列表（其余为纯文本）。"""
    spans: list[_Span] = []
    pos = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            spans.append(_Span(text[pos:m.start()]))
        if m.group("esc"):
            spans.append(_Span(m.group("esc")[1]))
        elif m.group("code"):
            spans.append(_Span(m.group("code_t"), code=True))
        elif m.group("del"):
            spans.append(_Span(m.group("del_t"), strike=True))
        elif m.group("strong"):
            spans.append(_Span(m.group("strong_t") or m.group("strong_u"),
                               bold=True))
        elif m.group("em"):
            spans.append(_Span(m.group("em_t") or m.group("em_u"),
                               italic=True))
        elif m.group("link"):
            spans.append(_Span(m.group("link_t") or m.group("link_u"),
                               href=m.group("link_u") or ""))
        pos = m.end()
    if pos < len(text):
        spans.append(_Span(text[pos:]))
    # `**粗体** 后的空 span 会生成空 run，Word 里无害但徒增体积
    return [s for s in spans if s.text]


_HEAD_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_HR_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_UL_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_OL_RE = re.compile(r"^(\s*)\d+[.)]\s+(.*)$")
_QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")


def _is_table_row(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.count("|") >= 2


def _cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _parse_blocks(md: str) -> list[_Block]:
    """Markdown → 块列表。支持的子集：标题/段落/引用/有序无序列表/代码块/表格/分隔线。"""
    lines = _xml_safe(md).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[_Block] = []
    para: list[str] = []
    i = 0
    n = len(lines)

    def flush() -> None:
        nonlocal para
        if para:
            text = "\n".join(para).strip()
            if text:
                blocks.append(_Block("para", spans=_split_inline(text)))
            para = []

    while i < n:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            flush()
            i += 1
            continue

        # 代码围栏
        if stripped.startswith("```") or stripped.startswith("~~~"):
            flush()
            fence = stripped[:3]
            lang = stripped[3:].strip()
            buf: list[str] = []
            i += 1
            while i < n and not lines[i].strip().startswith(fence):
                buf.append(lines[i])
                i += 1
            i += 1  # 跳过收尾围栏
            blocks.append(_Block("code", text="\n".join(buf), lang=lang))
            continue

        if _HR_RE.match(line):
            flush()
            blocks.append(_Block("hr"))
            i += 1
            continue

        m = _HEAD_RE.match(line)
        if m:
            flush()
            blocks.append(_Block("heading", level=len(m.group(1)),
                                 spans=_split_inline(m.group(2).strip())))
            i += 1
            continue

        # 表格：当前行是表格行且下一行是分隔行
        if _is_table_row(line) and i + 1 < n and _TABLE_SEP_RE.match(lines[i + 1]) \
                and "|" in lines[i + 1]:
            flush()
            rows: list[list[list[_Span]]] = []
            header = [_split_inline(c) for c in _cells(line)]
            i += 2
            while i < n and _is_table_row(lines[i]):
                rows.append([_split_inline(c) for c in _cells(lines[i])])
                i += 1
            blocks.append(_Block("table", rows=[header] + rows))
            continue

        m = _QUOTE_RE.match(line)
        if m:
            flush()
            buf = []
            while i < n:
                qm = _QUOTE_RE.match(lines[i])
                if qm:
                    buf.append(qm.group(1))
                    i += 1
                elif lines[i].strip() and not _looks_block_start(lines[i]):
                    buf.append(lines[i].strip())   # 引用内的续行
                    i += 1
                else:
                    break
            blocks.append(_Block("quote", spans=_split_inline("\n".join(buf).strip())))
            continue

        m = _UL_RE.match(line)
        if m:
            flush()
            items: list[list[_Span]] = []
            while i < n:
                im = _UL_RE.match(lines[i])
                if im:
                    items.append(_split_inline(im.group(2).strip()))
                    i += 1
                elif lines[i].strip() and not _looks_block_start(lines[i]):
                    # 列表项续行（缩进或非列表起手），并入上一项
                    if items:
                        items[-1].append(_Span("\n" + lines[i].strip()))
                    i += 1
                else:
                    break
            blocks.append(_Block("ul", items=items))
            continue

        m = _OL_RE.match(line)
        if m:
            flush()
            items = []
            while i < n:
                im = _OL_RE.match(lines[i])
                if im:
                    items.append(_split_inline(im.group(2).strip()))
                    i += 1
                elif lines[i].strip() and not _looks_block_start(lines[i]):
                    if items:
                        items[-1].append(_Span("\n" + lines[i].strip()))
                    i += 1
                else:
                    break
            blocks.append(_Block("ol", items=items))
            continue

        para.append(line)
        i += 1

    flush()
    return blocks


def _looks_block_start(line: str) -> bool:
    """该行是否开启一个新块（用于判断续行归属）。"""
    s = line.strip()
    return bool(_HEAD_RE.match(s) or _HR_RE.match(s) or s.startswith("```")
                or s.startswith("~~~") or _QUOTE_RE.match(line)
                or _UL_RE.match(line) or _OL_RE.match(line)
                or _is_table_row(s))


# ---- XML 生成

# ---- OOXML 元素顺序（严格）

# Word 对 pPr/rPr/tblPr 的子元素**顺序**敏感：乱序会直接报"文件损坏"，
# 而 XML 解析器不会报错——所以这类 bug 单元测试抓不到，必须靠顺序表兜住。
# 下表是 CT_PPr / CT_RPr / CT_TblPr 的 sequence 定义（ECMA-376 Part 1）。
_PPR_ORDER = (
    "pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr",
    "widowControl", "numPr", "suppressLineNumbers", "pBdr", "shd", "tabs",
    "suppressAutoHyphens", "kinsoku", "wordWrap", "overflowPunct",
    "topLinePunct", "autoSpaceDE", "autoSpaceDN", "bidi", "adjustRightInd",
    "snapToGrid", "spacing", "ind", "contextualSpacing", "mirrorIndents",
    "suppressOverlap", "jc", "textDirection", "textAlignment",
    "textboxTightWrap", "outlineLvl", "divId", "cnfStyle", "rPr", "sectPr",
    "pPrChange",
)
_RPR_ORDER = (
    "rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps", "strike",
    "dstrike", "outline", "shadow", "emboss", "imprint", "noProof",
    "snapToGrid", "vanish", "webHidden", "color", "spacing", "w", "kern",
    "position", "sz", "szCs", "highlight", "u", "effect", "bdr", "shd",
    "fitText", "vertAlign", "rtl", "cs", "em", "lang", "eastAsianLayout",
    "specVanish", "oMath",
)
_TBLPR_ORDER = (
    "tblStyle", "tblpPr", "tblOverlap", "bidiVisual", "tblStyleRowBandSize",
    "tblStyleColBandSize", "tblW", "jc", "tblCellSpacing", "tblInd",
    "tblBorders", "shd", "tblLayout", "tblCellMar", "tblLook",
)


def _rpr_xml(*, font: str, ascii_font: str, size: int, bold: bool = False,
             italic: bool = False, strike: bool = False, mono: bool = False,
             color: str = "", href: str = "", rstyle: str = "") -> str:
    """生成 w:rPr（元素顺序敏感，见 `_RPR_ORDER`：rStyle→rFonts→b→i→strike→color→sz→u）。

    **格式下沉原则**：标题加粗、引用斜体这类"整段一致"的格式走 `w:pStyle`
    （见 `_styles_xml`），不在 run 上写 `w:b`/`w:i`。否则 docx→md 读回时
    会被当成行内强调还原成 `**标题**`，往返不干净且污染 ingest 语料。
    """
    if mono:
        font = ascii_font = "Consolas"
    parts = []
    if rstyle:
        parts.append(f'<w:rStyle w:val="{_esc(rstyle)}"/>')
    parts.append(f'<w:rFonts w:ascii="{_esc(ascii_font)}" w:hAnsi="{_esc(ascii_font)}"'
                 f' w:eastAsia="{_esc(font)}" w:cs="{_esc(ascii_font)}"/>')
    if bold:
        parts.append("<w:b/><w:bCs/>")
    if italic:
        parts.append("<w:i/><w:iCs/>")
    if strike:
        parts.append("<w:strike/>")
    if href:
        color = "0563C1"          # 链接色覆盖传入色（color 须在 u 之前）
    if color:
        parts.append(f'<w:color w:val="{_esc(color)}"/>')
    parts.append(f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>')
    if href:
        # 超链接样式：下划线 + 蓝字。u 必须在 sz/szCs 之后（见 _RPR_ORDER）
        parts.append('<w:u w:val="single"/>')
    return "<w:rPr>" + "".join(parts) + "</w:rPr>"


def _runs_xml(spans: list[_Span], *, font: str, ascii_font: str, size: int,
              bold: bool = False, italic: bool = False, mono: bool = False,
              color: str = "", links: list[str] | None = None) -> str:
    """spans → 一串 <w:r>（超链接包 <w:hyperlink>）。

    段落内软换行（\\n）映射为 <w:br/>。带 href 的 span 需要在包内注册外部关系，
    因此 `links` 为收集器（顺序即 rId 分配顺序），传 None 表示不支持链接。
    """
    out: list[str] = []
    for sp in spans:
        if not sp.text:
            continue
        rpr = _rpr_xml(font=font, ascii_font=ascii_font, size=size,
                       bold=bold or sp.bold, italic=italic or sp.italic,
                       strike=sp.strike, mono=mono or sp.code,
                       color=color, href=sp.href,
                       rstyle="CodeChar" if sp.code else "")
        # 软换行拆成多个 run，中间插 <w:br/>
        segments = sp.text.split("\n")
        runs: list[str] = []
        for idx, seg in enumerate(segments):
            if idx:
                runs.append("<w:r><w:br/></w:r>")
            if seg:
                runs.append(f"<w:r>{rpr}<w:t xml:space=\"preserve\">{_esc(seg)}</w:t></w:r>")
        chunk = "".join(runs)
        if sp.href:
            if links is None:
                out.append(chunk)
            else:
                rid = f"rId{_FIRST_LINK_ID + len(links)}"
                links.append(sp.href)
                out.append(f'<w:hyperlink r:id="{rid}">{chunk}</w:hyperlink>')
        else:
            out.append(chunk)
    return "".join(out)


_TBL_BORDERS = (
    '<w:tblBorders>'
    '<w:top w:val="single" w:sz="4" w:space="0" w:color="888888"/>'
    '<w:left w:val="single" w:sz="4" w:space="0" w:color="888888"/>'
    '<w:bottom w:val="single" w:sz="4" w:space="0" w:color="888888"/>'
    '<w:right w:val="single" w:sz="4" w:space="0" w:color="888888"/>'
    '<w:insideH w:val="single" w:sz="4" w:space="0" w:color="888888"/>'
    '<w:insideV w:val="single" w:sz="4" w:space="0" w:color="888888"/>'
    '</w:tblBorders>'
)

# 空段落：相邻表格之间的分隔符（Word 不允许两个 w:tbl 直接相邻）
_EMPTY_P = '<w:p><w:pPr><w:spacing w:line="240" w:lineRule="auto" w:after="0"/></w:pPr></w:p>'


def _block_xml(b: _Block, *, cfg: "_WriteCfg", page_break: bool = False) -> str:
    font, afont = cfg.font_body, cfg.ascii_body
    if b.kind == "title":
        ppr = ('<w:pPr><w:pStyle w:val="Title"/>'
               '<w:spacing w:after="240"/>'
               '<w:jc w:val="center"/></w:pPr>')
        return f"<w:p>{ppr}{_runs_xml(b.spans, font=cfg.font_heading, ascii_font=cfg.ascii_heading, size=36, links=cfg.links)}</w:p>"

    if b.kind == "heading":
        size = {1: 32, 2: 28, 3: 26}.get(b.level, 24)
        style = f'<w:pStyle w:val="Heading{b.level}"/>' if b.level <= 6 else ""
        page = "<w:pageBreakBefore/>" if page_break else ""
        ppr = (f'<w:pPr>{style}{page}'
               f'<w:spacing w:before="240" w:after="120"/>'
               f'<w:jc w:val="left"/></w:pPr>')
        return f"<w:p>{ppr}{_runs_xml(b.spans, font=cfg.font_heading, ascii_font=cfg.ascii_heading, size=size, links=cfg.links)}</w:p>"

    if b.kind == "hr":
        ppr = ('<w:pPr><w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1"'
               ' w:color="999999"/></w:pBdr>'
               '<w:spacing w:after="120"/></w:pPr>')
        return f"<w:p>{ppr}</w:p>"

    if b.kind == "code":
        out = []
        for line in b.text.split("\n"):
            out.append(
                '<w:p><w:pPr><w:shd w:val="clear" w:fill="F2F2F2"/>'
                '<w:spacing w:line="240" w:lineRule="auto"/>'
                '<w:ind w:left="360" w:right="360"/></w:pPr>'
                + _runs_xml([_Span(line)], font=font, ascii_font=afont,
                            size=20, mono=True, links=cfg.links)
                + "</w:p>")
        return "".join(out)

    if b.kind == "quote":
        ppr = ('<w:pPr><w:pStyle w:val="Quote"/>'
               '<w:spacing w:before="120" w:after="120"/>'
               '<w:ind w:left="720" w:right="360"/></w:pPr>')
        return f"<w:p>{ppr}{_runs_xml(b.spans, font=cfg.font_quote, ascii_font=afont, size=22, links=cfg.links)}</w:p>"

    if b.kind in ("ul", "ol"):
        out = []
        for idx, item in enumerate(b.items, 1):
            marker = f"{idx}. " if b.kind == "ol" else "• "
            ppr = ('<w:pPr><w:spacing w:line="300" w:lineRule="auto"/>'
                   '<w:ind w:left="720" w:hanging="360"/></w:pPr>')
            spans = [_Span(marker)] + item
            out.append(f"<w:p>{ppr}{_runs_xml(spans, font=font, ascii_font=afont, size=22, links=cfg.links)}</w:p>")
        return "".join(out)

    if b.kind == "table":
        rows = b.rows
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        # 等宽分栏（twips 总和按 A4 可用宽度 8390 分配）
        col_w = max(int(8390 / width), 720)
        grid = "".join(f'<w:gridCol w:w="{col_w}"/>' for _ in range(width))
        tblpr = (f"<w:tblPr><w:tblW w:w=\"{col_w * width}\" w:type=\"dxa\"/>"
                 f'{_TBL_BORDERS}<w:tblLayout w:type="fixed"/></w:tblPr>')
        body = []
        for ri, row in enumerate(rows):
            cells = []
            for ci in range(width):
                spans = row[ci] if ci < len(row) else [_Span("")]
                shade = '<w:shd w:val="clear" w:fill="F2F2F2"/>' if ri == 0 else ""
                cpr = f'<w:tcPr><w:tcW w:w="{col_w}" w:type="dxa"/>{shade}</w:tcPr>'
                ppr = '<w:pPr><w:spacing w:before="40" w:after="40"/></w:pPr>'
                runs = _runs_xml(spans, font=font, ascii_font=afont, size=20,
                                 links=cfg.links)
                cells.append(f'<w:tc>{cpr}<w:p>{ppr}{runs}</w:p></w:tc>')
            trpr = '<w:trPr><w:tblHeader/></w:trPr>' if ri == 0 else ""
            body.append(f"<w:tr>{trpr}" + "".join(cells) + "</w:tr>")
        return f"<w:tbl>{tblpr}<w:tblGrid>{grid}</w:tblGrid>" + "".join(body) + "</w:tbl>"

    # 正文段落
    ind = ('<w:ind w:firstLineChars="200" w:firstLine="480"/>'
           if cfg.indent else "")
    ppr = (f'<w:pPr><w:spacing w:line="360" w:lineRule="auto" w:after="60"/>'
           f'{ind}<w:jc w:val="both"/></w:pPr>')
    return f"<w:p>{ppr}{_runs_xml(b.spans, font=font, ascii_font=afont, size=22, links=cfg.links)}</w:p>"


@dataclass
class _WriteCfg:
    font_body: str = "宋体"
    font_heading: str = "黑体"
    font_quote: str = "楷体"
    ascii_body: str = "Times New Roman"
    ascii_heading: str = "Arial"
    indent: bool = True
    chapter_page_break: bool = True
    # 外部超链接收集器：顺序即 rId 分配顺序（见 _FIRST_LINK_ID）
    links: list[str] = field(default_factory=list)


def _styles_xml(cfg: _WriteCfg) -> str:
    """最小样式表：Normal / Title / Heading1-6 / Quote。

    不引外部主题字体表（`themeFontLang` 缺省即使用 rFonts 直给值），Word 打开正常。
    """
    def base(sid: str, name: str, *, size: int, font: str, afont: str,
             bold: bool = False, italic: bool = False,
             jc: str = "", spacing: str = "", default: bool = False,
             outline: int | None = None, color: str = "") -> str:
        rpr = _rpr_xml(font=font, ascii_font=afont, size=size, bold=bold,
                       italic=italic, color=color)
        ppr_bits = []
        if spacing:
            ppr_bits.append(spacing)
        if jc:
            ppr_bits.append(f'<w:jc w:val="{jc}"/>')
        if outline is not None:
            ppr_bits.append(f'<w:outlineLvl w:val="{outline}"/>')
        ppr = f"<w:pPr>{''.join(ppr_bits)}</w:pPr>" if ppr_bits else ""
        dflt = '<w:default w:val="1"/>' if default else ""
        return (f'<w:style w:type="paragraph" w:styleId="{sid}">'
                f'<w:name w:val="{_esc(name)}"/>{dflt}{ppr}{rpr}</w:style>')

    parts = [
        base("Normal", "Normal", size=22, font=cfg.font_body,
             afont=cfg.ascii_body, default=True,
             spacing='<w:spacing w:line="360" w:lineRule="auto"/>'),
        base("Title", "Title", size=36, font=cfg.font_heading,
             afont=cfg.ascii_heading, bold=True, jc="center",
             outline=0, spacing='<w:spacing w:after="240"/>'),
        base("Quote", "Quote", size=22, font=cfg.font_quote,
             afont=cfg.ascii_body, italic=True, color="555555",
             spacing='<w:spacing w:before="120" w:after="120"/>'),
    ]
    sizes = {1: 32, 2: 28, 3: 26, 4: 24, 5: 24, 6: 24}
    for lvl in range(1, 7):
        parts.append(base(f"Heading{lvl}", f"heading {lvl}", size=sizes[lvl],
                          font=cfg.font_heading, afont=cfg.ascii_heading,
                          bold=True, outline=lvl - 1,
                          spacing='<w:spacing w:before="240" w:after="120"/>'))
    # 字符样式：行内代码。写侧打标记，读侧据此还原反引号（往返一致）
    code_rpr = _rpr_xml(font="Consolas", ascii_font="Consolas", size=20, mono=True)
    parts.append('<w:style w:type="character" w:styleId="CodeChar">'
                 '<w:name w:val="Code Char"/>'
                 f"{code_rpr}</w:style>")
    return (f'<w:styles xmlns:w="{_W}">' + "".join(parts) + "</w:styles>")


def _content_types_xml() -> str:
    return (
        f'<Types xmlns="{_CT}">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        "</Types>")


def _root_rels_xml() -> str:
    return (f'<Relationships xmlns="{_PR}">'
            f'<Relationship Id="rId1" Type="{_RT_DOC}" Target="word/document.xml"/>'
            f'<Relationship Id="rId2" Type="{_RT_CORE}" Target="docProps/core.xml"/>'
            f'<Relationship Id="rId3" Type="{_RT_EXT}" Target="docProps/app.xml"/>'
            "</Relationships>")


def _doc_rels_xml(links: list[str] | None = None) -> str:
    """document.xml.rels：rId1 = styles，其后为外部超链接（TargetMode=External）。"""
    out = [f'<Relationship Id="rId1" Type="{_RT_STYLES}" Target="styles.xml"/>']
    for i, url in enumerate(links or []):
        out.append(f'<Relationship Id="rId{_FIRST_LINK_ID + i}" Type="{_RT_HYPERLINK}"'
                   f' Target="{_esc(url)}" TargetMode="External"/>')
    return f'<Relationships xmlns="{_PR}">' + "".join(out) + "</Relationships>"


def _core_xml(title: str) -> str:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (f'<cp:coreProperties xmlns:cp="{_CP}" xmlns:dc="{_DC}"'
            f' xmlns:dcterms="{_DCTERMS}">'
            f"<dc:title>{_esc(title)}</dc:title>"
            "<dc:creator>Novelist</dc:creator>"
            f"<cp:lastModifiedBy>Novelist</cp:lastModifiedBy>"
            f'<dcterms:created xsi:type="dcterms:W3CDTF" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">{now}</dcterms:created>'
            f'<dcterms:modified xsi:type="dcterms:W3CDTF" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">{now}</dcterms:modified>'
            "</cp:coreProperties>")


def _app_xml() -> str:
    return (f'<Properties xmlns="{_EP}">'
            "<Application>Novelist</Application>"
            "</Properties>")


def markdown_to_docx(md: str, dst: str | Path, *, title: str | None = None,
                     font_body: str = "宋体", font_heading: str = "黑体",
                     ascii_body: str = "Times New Roman",
                     ascii_heading: str = "Arial",
                     indent: bool = True, chapter_page_break: bool = True,
                     title_style: bool = True) -> Path:
    """Markdown → Word(.docx)，返回输出路径。

    - 首个 H1 渲染为 Title（居中、18pt、黑体），其后每个 H1 段前分页（书稿习惯）
    - 正文：宋体小四、1.5 倍行距、两端对齐、**首行缩进两字符**
    - 表格原生 `w:tbl`（不是等宽文本）；列表用 `•` / `1.` 前缀 + 悬挂缩进
    - `indent=False` 关闭首行缩进（网文库/网页发布场景）
    """
    cfg = _WriteCfg(font_body=font_body, font_heading=font_heading,
                    ascii_body=ascii_body, ascii_heading=ascii_heading,
                    indent=indent, chapter_page_break=chapter_page_break)
    blocks = _parse_blocks(md)

    body: list[str] = []
    doc_title = title or (_first_h1(blocks) if title_style else "") or "Novelist 导出"

    # 标题页：显式 title 优先；否则首个 H1 升格为书名页（书稿惯例）
    title_emitted = False
    if title:
        body.append(_block_xml(_Block("title", spans=[_Span(title)]), cfg=cfg))
        title_emitted = True

    for b in blocks:
        if (title_style and not title and not title_emitted
                and b.kind == "heading" and b.level == 1):
            body.append(_block_xml(_Block("title", spans=b.spans), cfg=cfg))
            title_emitted = True
            continue
        page_break = (cfg.chapter_page_break and title_emitted
                      and b.kind == "heading" and b.level == 1)
        xml = _block_xml(b, cfg=cfg, page_break=page_break)
        # 相邻 w:tbl 之间必须插一个空段落，否则 Word 判定包损坏
        if xml.startswith("<w:tbl>"):
            if body and body[-1].startswith("<w:tbl>"):
                body.append(_EMPTY_P)
        body.append(xml)

    sect = ('<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
            '<w:pgMar w:top="1440" w:right="1418" w:bottom="1440"'
            ' w:left="1418" w:header="851" w:footer="992" w:gutter="0"/>'
            "</w:sectPr>")
    document = (f'<w:document xmlns:w="{_W}" xmlns:r="{_R}"><w:body>'
                + "".join(body) + sect + "</w:body></w:document>")

    out = Path(dst)
    if out.parent and str(out.parent) not in ("", "."):
        out.parent.mkdir(parents=True, exist_ok=True)
    # 固定时间戳：同内容两次导出字节一致（便于 diff / 校验和）
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _content_types_xml())
        z.writestr("_rels/.rels", _root_rels_xml())
        z.writestr("word/document.xml", document)
        z.writestr("word/styles.xml", _styles_xml(cfg))
        z.writestr("word/_rels/document.xml.rels", _doc_rels_xml(cfg.links))
        z.writestr("docProps/core.xml", _core_xml(doc_title))
        z.writestr("docProps/app.xml", _app_xml())
    return out


def _first_h1(blocks: list[_Block]) -> str:
    for b in blocks:
        if b.kind == "heading":
            return "".join(s.text for s in b.spans)
    return ""


# ==================================================================== 分派

def convert(src: str | Path, dst: str | Path, **kw) -> Path:
    """按后缀自动判定方向：.docx→.md 或 .md→.docx。"""
    s, d = Path(src), Path(dst)
    ss, ds = s.suffix.lower(), d.suffix.lower()
    if ss in (".docx", ".doc") and ds in (".md", ".markdown", ".txt"):
        text = docx_to_markdown(s, **kw)
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_text(text, encoding="utf-8")
        return d
    if ss in (".md", ".markdown", ".txt") and ds == ".docx":
        return markdown_to_docx(s.read_text(encoding="utf-8"), d, **kw)
    raise DocxConvError(f"无法判定转换方向：{s.name} → {d.name}"
                        f"（需一侧 .docx、另一侧 .md）")
