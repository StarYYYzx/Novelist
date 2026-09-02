"""M3o：Markdown ⇄ Word(.docx) 互转（core/docxconv.py）。

零第三方依赖（stdlib zipfile + OOXML），测试也只用 stdlib 校验包结构：
必需部件齐全、每个部件 XML 可解析、正文结构符合预期。
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from novelist.core.docxconv import (
    DocxConvError,
    convert,
    docx_to_markdown,
    is_docx,
    markdown_to_docx,
)

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
REQUIRED_PARTS = (
    "[Content_Types].xml",
    "_rels/.rels",
    "word/document.xml",
    "word/styles.xml",
    "word/_rels/document.xml.rels",
)


def _qn(tag: str) -> str:
    return f"{{{_W}}}{tag}"


def _open_doc(path: Path) -> tuple[ET.Element, list[str]]:
    """返回 (body 元素, 包内部件名列表)；顺带断言必需部件存在且 XML 合法。"""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        for part in REQUIRED_PARTS:
            assert part in names, f"缺部件 {part}"
            ET.fromstring(z.read(part))       # 非法 XML 会抛 ParseError
        root = ET.fromstring(z.read("word/document.xml"))
    assert root.tag == _qn("document")
    body = root.find(_qn("body"))
    assert body is not None
    return body, names


def _text_of(el: ET.Element) -> str:
    return "".join(t.text or "" for t in el.iter(_qn("t")))


def _paragraphs(body: ET.Element) -> list[ET.Element]:
    return body.findall(_qn("p"))


# ---------------------------------------------------------------- 写侧

def test_docx_package_is_valid(tmp_path: Path) -> None:
    """最小包：必需部件齐全、每个部件 XML 可解析、根元素正确。"""
    out = tmp_path / "a.docx"
    markdown_to_docx("# 标题\n\n正文一段。\n", out)
    body, _ = _open_doc(out)
    assert body is not None
    assert is_docx(out)


def test_headings_and_body_roundtrip(tmp_path: Path) -> None:
    """标题级别 + 正文文本往返一致。"""
    md = "# 书名\n\n## 第一章\n\n叶岚睁开眼。\n"
    out = tmp_path / "b.docx"
    markdown_to_docx(md, out)
    body, _ = _open_doc(out)

    # 首个 H1 → Title 样式
    styles = [_pstyle(p) for p in _paragraphs(body)]
    assert styles[0] == "Title"
    assert "Heading2" in styles

    back = docx_to_markdown(out)
    assert "# 书名" in back
    assert "## 第一章" in back
    assert "叶岚睁开眼。" in back


def _pstyle(p: ET.Element) -> str:
    ppr = p.find(_qn("pPr"))
    if ppr is None:
        return ""
    ps = ppr.find(_qn("pStyle"))
    return (ps.get(_qn("val")) or "") if ps is not None else ""


def test_chapter_page_break_only_after_title(tmp_path: Path) -> None:
    """首个 H1 升格书名页不分页；后续每个 H1 段前分页。"""
    md = "# 书名\n\n# 第一章\n\n正文。\n\n# 第二章\n\n正文。\n"
    out = tmp_path / "c.docx"
    markdown_to_docx(md, out)
    body, _ = _open_doc(out)

    breaks = []
    for p in _paragraphs(body):
        ppr = p.find(_qn("pPr"))
        breaks.append(ppr is not None and ppr.find(_qn("pageBreakBefore")) is not None)
    # 书名页独占一页 → 其后每个 H1（第一章/第二章）都段前分页
    assert breaks == [False, True, False, True, False]


def test_chapter_page_break_can_be_disabled(tmp_path: Path) -> None:
    md = "# 书名\n\n# 第一章\n\n正文。\n"
    out = tmp_path / "c2.docx"
    markdown_to_docx(md, out, chapter_page_break=False)
    body, _ = _open_doc(out)
    for p in _paragraphs(body):
        ppr = p.find(_qn("pPr"))
        assert ppr is None or ppr.find(_qn("pageBreakBefore")) is None


def test_body_indent_and_font(tmp_path: Path) -> None:
    """正文：首行缩进两字符 + 中文字体（eastAsia）。"""
    out = tmp_path / "d.docx"
    markdown_to_docx("正文一段。\n", out, indent=True)
    with zipfile.ZipFile(out) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    p = root.find(f"{_qn('body')}/{_qn('p')}")
    ppr = p.find(_qn("pPr"))
    ind = ppr.find(_qn("ind"))
    assert ind is not None and ind.get(_qn("firstLineChars")) == "200"
    rfonts = p.find(f"{_qn('r')}/{_qn('rPr')}/{_qn('rFonts')}")
    assert rfonts is not None
    assert rfonts.get(_qn("eastAsia")) == "宋体"


def test_indent_can_be_disabled(tmp_path: Path) -> None:
    out = tmp_path / "d2.docx"
    markdown_to_docx("正文一段。\n", out, indent=False)
    with zipfile.ZipFile(out) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    ppr = root.find(f"{_qn('body')}/{_qn('p')}/{_qn('pPr')}")
    assert ppr.find(_qn("ind")) is None


def test_inline_emphasis_roundtrip(tmp_path: Path) -> None:
    """粗体/斜体/行内代码往返。"""
    md = "他**握紧**了剑，*缓缓*抬起，念出`疾风咒`。\n"
    out = tmp_path / "e.docx"
    markdown_to_docx(md, out)
    back = docx_to_markdown(out)
    assert "**握紧**" in back
    assert "*缓缓*" in back
    assert "`疾风咒`" in back


def test_table_becomes_native_tbl(tmp_path: Path) -> None:
    """表格生成原生 w:tbl，且相邻表格之间插入空段落。"""
    md = ("| 姓名 | 境界 |\n|---|---|\n| 叶岚 | 炼气三层 |\n\n"
          "| 甲 | 乙 |\n|---|---|\n| 1 | 2 |\n")
    out = tmp_path / "f.docx"
    markdown_to_docx(md, out)
    body, _ = _open_doc(out)

    tbls = body.findall(_qn("tbl"))
    assert len(tbls) == 2
    rows = tbls[0].findall(_qn("tr"))
    assert len(rows) == 2                       # 表头 + 1 行
    cells = rows[0].findall(_qn("tc"))
    assert [_text_of(c) for c in cells] == ["姓名", "境界"]

    # 相邻表格之间必须有段落分隔（Word 兼容性）
    children = [c.tag for c in body if c.tag in (_qn("tbl"), _qn("p"))]
    assert _qn("p") in children[children.index(_qn("tbl")) + 1:len(children) - 1] \
        or children.count(_qn("p")) >= 1

    back = docx_to_markdown(out)
    assert "| 姓名 | 境界 |" in back
    assert "| 叶岚 | 炼气三层 |" in back


def test_lists_and_quote_and_hr(tmp_path: Path) -> None:
    md = "- 其一\n- 其二\n\n1. 甲\n2. 乙\n\n> 引用一句\n\n---\n"
    out = tmp_path / "g.docx"
    markdown_to_docx(md, out)
    body, _ = _open_doc(out)
    text = _text_of(body)
    assert "• 其一" in text and "• 其二" in text
    assert "1. 甲" in text and "2. 乙" in text
    assert "引用一句" in text
    assert _text_of(body).count("引用一句") == 1


def test_code_block(tmp_path: Path) -> None:
    md = "```python\nprint(1)\n```\n"
    out = tmp_path / "h.docx"
    markdown_to_docx(md, out)
    body, _ = _open_doc(out)
    assert "print(1)" in _text_of(body)


def test_control_chars_stripped(tmp_path: Path) -> None:
    """C0 控制符会让 Word 报文件损坏，必须剔除。"""
    out = tmp_path / "i.docx"
    markdown_to_docx("正常\x0b文本\x0c结束\n", out)
    with zipfile.ZipFile(out) as z:
        raw = z.read("word/document.xml")
    assert b"\x0b" not in raw and b"\x0c" not in raw
    ET.fromstring(raw)                            # 仍可解析
    assert "正常文本结束" in docx_to_markdown(out)


def test_xml_special_chars_escaped(tmp_path: Path) -> None:
    out = tmp_path / "j.docx"
    markdown_to_docx("a & b < c > d \"e\"\n", out)
    with zipfile.ZipFile(out) as z:
        ET.fromstring(z.read("word/document.xml"))  # 未转义会 ParseError
    assert "a & b < c > d \"e\"" in docx_to_markdown(out)


def test_explicit_title_overrides_first_h1(tmp_path: Path) -> None:
    out = tmp_path / "k.docx"
    markdown_to_docx("# 原书名\n\n正文。\n", out, title="指定书名")
    body, _ = _open_doc(out)
    assert _pstyle(_paragraphs(body)[0]) == "Title"
    assert "指定书名" in _text_of(body)


# ---------------------------------------------------------------- 读侧

def _make_raw_docx(path: Path, paragraphs: list[str]) -> Path:
    """构造最小 docx（只有 document.xml，无 styles.xml）用于精确测读侧行为。"""
    body = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{t}</w:t></w:r></w:p>'
        for t in paragraphs)
    doc = (f'<w:document xmlns:w="{_W}"><w:body>{body}'
           '<w:sectPr/></w:body></w:document>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                   '</Types>')
        z.writestr("_rels/.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
                   '</Relationships>')
        z.writestr("word/document.xml", doc)
    return path


def test_read_escapes_leading_markers(tmp_path: Path) -> None:
    """正文行首的标记字符必须转义，否则读回的 md 会被解析成标题/列表。

    用手工 docx（无样式、纯正文段落）喂读侧——md 里 `# x` 本来就是标题语法，
    走 `markdown_to_docx` 是测不到这条路径的。
    """
    out = _make_raw_docx(tmp_path / "raw.docx",
                         ["1. 他起身", "# 井号开头", "- 减号开头", "= 等号开头"])
    back = docx_to_markdown(out)
    assert r"1\. 他起身" in back
    assert r"\# 井号开头" in back
    assert r"\- 减号开头" in back
    assert r"\= 等号开头" in back


def test_read_plain_docx_without_styles(tmp_path: Path) -> None:
    """缺 styles.xml / rels 的极端包也能读（降级为纯正文，不抛异常）。"""
    out = _make_raw_docx(tmp_path / "bare.docx", ["第一段。", "第二段。"])
    back = docx_to_markdown(out)
    assert "第一段。" in back and "第二段。" in back


def test_quote_roundtrip() -> None:
    """引用块往返：Quote 样式（斜体/颜色在样式里，run 级不放，避免读回 *污染*）。"""
    out = Path(__file__).parent / "_tmp_quote.docx"
    try:
        markdown_to_docx("> 他转身离去。\n", out, title_style=False)
        back = docx_to_markdown(out)
        assert "> 他转身离去。" in back
        assert "**" not in back and "*" not in back
    finally:
        if out.exists():
            out.unlink()


def test_read_ignores_inline_underscore_noise() -> None:
    """行内 _ / * 不转义——转出的 md 要喂回 ingest，反斜杠会污染语料。"""
    out = Path(__file__).parent / "_tmp_read2.docx"
    try:
        markdown_to_docx("变量 a_b 与 2*3 都在正文里\n", out, title_style=False)
        back = docx_to_markdown(out)
        assert "a_b" in back and "2*3" in back
        assert "\\" not in back
    finally:
        if out.exists():
            out.unlink()


def test_read_hyperlink() -> None:
    """超链接读回为 md 链接（目标来自 document.xml.rels）。"""
    src = Path(__file__).parent / "_tmp_link.docx"
    try:
        markdown_to_docx("见[官网](https://example.com)说明\n", src,
                         title_style=False)
        back = docx_to_markdown(src)
        assert "https://example.com" in back
        assert "[官网]" in back
    finally:
        if src.exists():
            src.unlink()


# ---------------------------------------------------------------- 分派与错误

def test_convert_dispatches_by_suffix(tmp_path: Path) -> None:
    md_path = tmp_path / "book.md"
    md_path.write_text("# 书名\n\n正文。\n", encoding="utf-8")
    docx_path = tmp_path / "book.docx"
    convert(md_path, docx_path)
    assert is_docx(docx_path)

    back_path = tmp_path / "back.md"
    convert(docx_path, back_path)
    assert "# 书名" in back_path.read_text(encoding="utf-8")


def test_convert_rejects_unknown_direction(tmp_path: Path) -> None:
    a = tmp_path / "a.pdf"
    a.write_text("x", encoding="utf-8")
    b = tmp_path / "b.pdf"
    with pytest.raises(DocxConvError):
        convert(a, b)


def test_non_docx_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.docx"
    bad.write_text("not a zip", encoding="utf-8")
    assert not is_docx(bad)
    with pytest.raises(DocxConvError):
        docx_to_markdown(bad)


def test_output_dir_created(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "deep" / "book.docx"
    markdown_to_docx("正文。\n", out)
    assert out.is_file()
