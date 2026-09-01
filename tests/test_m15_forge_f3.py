"""M3l F3 测试（docs/10 §6 / docs/08 F3）：模式二 ingest——已有稿子 → 蓝图 + 接着写。

覆盖（**单测绝不真调 LLM**，docs/09 §2.1——FakeProvider/行格式 Stub/注入 IO）：
- 切片：`第X章`/`Chapter N` 标题行优先（容忍数字与章节字间空格）、无标题按字数兜底、空文件跳过
- 预览确认：非 TTY 直过 / 回车全确认 / q 退出（不写库）/ N 从第 N 章重切 / R 全按字数重切
- 确定性抽取：2/3 字名归并与组织通名过滤、专名动词/虚词前缀截断、pending 双语序、指标
- 归并消歧：序号 id 唯一（中文名不可作 id）、频次 ≥3 进主线、主角判定、别名合并
- run_ingest：无 provider 纯确定性 + 降级披露（LLM 失败 / 配额耗尽）、全链路（正式章节 +
  细纲 done=true + 文风 provenance=ingested + worldstate pending + stage=ingested）、
  dry_run 不写库、interactive 无 provider 回落商讨跳过
- 记忆初始化：行格式 provider → memory/plot_events.json 有事件
- CLI：`forge ingest` fake 纯确定性链路 + 下一步提示；--dry-run 只预览
"""

from __future__ import annotations

import json

import pytest

from novelist.core.llm import LLMResult
from novelist.forge import Blueprint, ForgeState, run_ingest
from novelist.forge.ingest import (
    ChapterSlice,
    extract_deterministic,
    init_memory,
    merge_characters,
    preview_chapters,
    slice_chapters,
)
from novelist.forge.state import read_transcript
from novelist.providers.fake import FakeProvider


class FakeIO:
    """可注入的问答通道替身（is_tty 可设，输入可脚本化）。"""

    def __init__(self, lines: list[str] | None = None, *, is_tty: bool = True) -> None:
        self.lines = list(lines or [])
        self.is_tty = is_tty
        self.out: list[str] = []
        self.asked: list[str] = []

    def notify(self, text: str) -> None:
        self.out.append(text)

    def ask_free(self, prompt: str, default: str = "") -> str | None:
        self.asked.append(prompt)
        return self.lines.pop(0) if self.lines else ""

    def ask_choice(self, prompt: str, options: list[str], default_idx: int = 0) -> int | None:
        return default_idx

    def confirm(self, prompt: str, default: bool = True) -> bool:
        return default


class LineProvider:
    """行格式回复替身（chronicler 解析 `事件：摘要|kind|人名` 行，非 JSON）。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    def complete(self, req) -> LLMResult:
        self.calls += 1
        return LLMResult(ok=True, content=self.text, finish_reason="stop", blocked=False)


def _write_draft(tmp_path, text: str, name: str = "draft.md"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _chunks(*items: tuple[str, str]) -> list[ChapterSlice]:
    return [ChapterSlice(title=t, text=b) for t, b in items]


DRAFT = """第 1 章 初入宗门

陆沉在青云宗外门修行三年，与陆沉同门的还有林远。一日，陆沉服用洗髓丹，药力入体，冲击练气三层。
三日后闭关，冲击筑基。宗门上下震动。

第 2 章 出关

林远闭关三月后出关，境界大涨。渡劫九年后失踪，宗门上下搜寻未果。陆沉望着天际，想起洗髓丹的药力。
"""

EXTRACT_REPLY = json.dumps({
    "characters": [{"name": "陆沉", "gender": "male", "realm": "练气三层",
                    "traits": ["坚毅"], "aliases": ["陆师弟"], "relation": "主角"},
                   {"name": "林远", "gender": "male", "realm": "筑基一层",
                    "traits": ["沉稳"], "aliases": [], "relation": "同门"}],
    "realms": ["练气三层", "筑基"],
    "locations": [{"name": "青云宗", "kind": "宗门"}],
    "items": [{"name": "洗髓丹", "kind": "丹药"}],
    "key_events": ["陆沉服用洗髓丹突破练气三层"],
    "pending": ["三日后闭关"],
    "foreshadowing": [],
    "style_notes": "节奏明快，对白少",
}, ensure_ascii=False)


# ============================================================ 1. 切片


def test_slice_headers_priority(tmp_path):
    """标题行切章优先（`第X章` 容忍数字后空格；Chapter N 混用）。"""
    text = "第 1 章 初入\n正文一\n\nChapter 2 出关\n正文二"
    src = _write_draft(tmp_path, text)
    out, warns = slice_chapters(src)
    assert warns == []
    assert [c.title for c in out] == ["第 1 章 初入", "Chapter 2 出关"]
    assert "正文一" in out[0].text and "正文二" in out[1].text


def test_slice_word_fallback(tmp_path):
    """无标题行 → 空行段落 + 目标字数兜底切。"""
    src = _write_draft(tmp_path, "段落一。\n\n段落二。\n\n段落三。")
    out, _ = slice_chapters(src, target_words=8)  # 段1+段2 累计 9 字 ≥8 → 切；段3 残留
    assert len(out) == 2
    assert out[0].title == "第 1 段"
    # target 足够小 → 按段切多章
    src2 = _write_draft(tmp_path, "段落一。\n\n段落二。\n\n段落三。", name="d2.md")
    out2, _ = slice_chapters(src2, target_words=3)
    assert len(out2) == 3
    assert out2[0].title == "第 1 段"


def test_slice_empty_file_skipped(tmp_path):
    _write_draft(tmp_path, "", name="empty.md")
    src = _write_draft(tmp_path, "第 1 章 X\n正文")
    out, warns = slice_chapters(src)
    assert len(out) == 1
    assert warns == []  # empty.md 不在传入的 src 里（单文件源）


def test_slice_dir_recursive(tmp_path):
    (tmp_path / "sub").mkdir()
    _write_draft(tmp_path / "sub", "第 1 章 A\n正文A", name="a.md")
    _write_draft(tmp_path, "第 1 章 B\n正文B", name="b.txt")
    out, _ = slice_chapters(tmp_path, recursive=True)
    assert [c.src for c in out] == ["b.txt", "a.md"]  # sorted 跨目录
    assert len(out) == 2


def test_slice_missing_source_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        slice_chapters(str(tmp_path / "nope.md"))


# ============================================================ 2. 预览确认


def test_preview_notty_auto_confirm():
    io = FakeIO(is_tty=False)
    cs = _chunks(("第 1 章", "正文一"), ("第 2 章", "正文二"))
    assert preview_chapters(io, cs, 100) is cs


def test_preview_enter_confirms_all():
    io = FakeIO(lines=[""])
    cs = _chunks(("第 1 章", "正文一"))
    assert preview_chapters(io, cs, 100) == cs
    assert io.out  # 预览被打印


def test_preview_q_aborts():
    io = FakeIO(lines=["q"])
    assert preview_chapters(io, _chunks(("第 1 章", "正文一")), 100) == []


def test_preview_n_reslices_from_chapter():
    """N 输入 → 从第 N 章起重切（按字数），前 N-1 章保留。"""
    io = FakeIO(lines=["2"])
    cs = _chunks(("第 1 章", "正文A"), ("第 2 章", "正文B。正文C。正文D。"))
    out = preview_chapters(io, cs, 5)
    assert len(out) == 2  # 第 1 章保留；第 2 章正文为单段 → 整段 1 章（标题重切为「第 1 段」）
    assert out[0].title == "第 1 章"
    assert out[1].title == "第 1 段"
    assert "正文D" in out[1].text


def test_preview_r_reslices_all():
    io = FakeIO(lines=["r", ""])
    cs = _chunks(("第 1 章", "正文A。正文B。"), ("第 2 章", "正文C。正文D。"))
    out = preview_chapters(io, cs, 5)
    # 4 段各 4 字：两段累计 8 ≥5 → 每 2 段 1 章
    assert [c.title for c in out] == ["第 1 段", "第 2 段"]
    assert "正文D" in out[-1].text


# ============================================================ 3. 确定性抽取


def test_extract_names_dedup_and_org_stop():
    text = ("陆沉与林远同门。陆沉服用丹药。林远闭关。宗门上下震动。弟子们议论纷纷。"
            "叶蓝心出手相助。叶蓝心收徒。叶蓝心离开。")
    lex = {"address": ["道友", "师兄"]}
    r = extract_deterministic(text, lex)
    names = dict(r["names"])
    assert names["陆沉"] == 2 and names["林远"] == 2
    assert names["叶蓝心"] == 3  # 3 字名独立保留（前 2 字非 2 字名）
    assert "宗门" not in names and "弟子" not in names  # 组织/身份通名过滤


def test_extract_items_prefix_trimmed():
    text = "陆沉服用洗髓丹。上有洗髓丹残香。炼化落霞剑成功。"
    lex = {"item_suffix": ["丹", "剑"]}
    r = extract_deterministic(text, lex)
    assert r["items"] == ["洗髓丹", "落霞剑"]


def test_extract_pending_both_orders():
    text = "三日后闭关，冲击筑基。林远闭关三月后出关。渡劫九年后失踪。沉睡百日方醒。"
    r = extract_deterministic(text)
    pending = "\n".join(r["pending_lines"])
    assert "三日后闭关｜+3日" in pending
    assert "闭关三月后｜+90日" in pending
    assert "渡劫九年后｜+3285日" in pending
    assert "沉睡百日｜+100日" in pending  # what 取匹配串（状态词+时长），不吞后续动词


def test_extract_stats():
    text = "“来战。”陆沉道。“好。”林远答。陆沉一剑挥出，剑气纵横三千里。"
    r = extract_deterministic(text)
    assert r["dialogue_ratio"] > 0
    assert r["sentence_stats"]["mean"] > 0


# ============================================================ 4. 归并消歧


def test_merge_characters_unique_ids_and_protagonist():
    bp = Blueprint.blank()
    extracts = [{
        "characters": [{"name": "陆沉", "gender": "male", "relation": "主角"},
                       {"name": "林远", "gender": "male", "aliases": ["林师弟"]}],
    }]
    det = {"陆沉": 4, "林远": 2, "叶蓝心": 3}
    r = merge_characters(bp, extracts, det)
    chars = bp.section("characters")
    ids = [c["id"] for c in chars]
    assert len(ids) == len(set(ids))  # 序号 id 唯一
    assert all(i.startswith("char:in") for i in ids)  # ASCII 合法（schema）
    by_name = {c["name"]: c for c in chars}
    assert by_name["陆沉"]["role"] == "protagonist"
    assert "林师弟" in by_name["林远"]["aliases"]
    assert r["protagonist"].startswith("char:")


def test_merge_characters_low_freq_semantic_skip():
    bp = Blueprint.blank()
    extracts = []  # 无语义信息
    det = {"路人甲": 1, "陆沉": 5}
    r = merge_characters(bp, extracts, det)
    chars = bp.section("characters")
    assert [c["name"] for c in chars] == ["陆沉"]  # 低频无 gender 不建档
    assert r["protagonist"] == chars[0]["id"]  # 无明示 → 最高频者


# ============================================================ 5. run_ingest：降级与披露


def test_ingest_provider_none_pure_deterministic(ws_factory, tmp_path):
    ws, pid = ws_factory("proj-f3a")
    src = _write_draft(tmp_path, DRAFT)
    res = run_ingest(ws, pid, str(src), provider=None, genre="修仙",
                     chapters_per_volume=20, target_words=100,
                     mode="auto", io=FakeIO(is_tty=False))
    assert res.ok and res.chapters_ingested == 2
    assert res.downgraded == [1, 2]  # 无 provider 全部降级纯确定性
    assert any("无 LLM provider" in w or "降级" in w for w in res.warnings)
    assert res.calls_used == 0


def test_ingest_llm_failure_downgrades(ws_factory, tmp_path):
    ws, pid = ws_factory("proj-f3b")
    src = _write_draft(tmp_path, DRAFT)
    res = run_ingest(ws, pid, str(src), provider=FakeProvider(reply="不是 JSON"),
                     chapters_per_volume=20, target_words=100, ingest_max_calls=30,
                     mode="auto", io=FakeIO(is_tty=False))
    assert res.ok
    assert res.downgraded == [1, 2]
    assert any("降级确定性" in w for w in res.warnings)


def test_ingest_quota_exhausted_downgrades_later(ws_factory, tmp_path):
    ws, pid = ws_factory("proj-f3c")
    src = _write_draft(tmp_path, DRAFT)
    res = run_ingest(ws, pid, str(src), provider=FakeProvider(reply=EXTRACT_REPLY),
                     chapters_per_volume=20, target_words=100, ingest_max_calls=1,
                     mode="auto", io=FakeIO(is_tty=False))
    assert res.downgraded == [2]  # 第 1 章用掉配额，第 2 章降级
    assert any("配额" in w for w in res.warnings)
    assert res.calls_used >= 1  # 抽取 1 + chronicler 无余量


# ============================================================ 6. run_ingest：全链路


def test_ingest_full_pipeline(ws_factory, tmp_path):
    ws, pid = ws_factory("proj-f3d")
    src = _write_draft(tmp_path, DRAFT)
    res = run_ingest(ws, pid, str(src), provider=FakeProvider(reply=EXTRACT_REPLY),
                     genre="修仙", chapters_per_volume=20, target_words=100,
                     mode="auto", io=FakeIO(is_tty=False))
    assert res.ok
    assert res.chapters_ingested == 2 and res.volumes_encoded == 1
    assert res.calls_used == 4  # 2 抽取 + 2 chronicler
    assert res.downgraded == []

    # 卷章编码：正式章节 + 细纲反写 done=true
    ch1 = ws.chapter_path(pid, 1, 1)
    ch2 = ws.chapter_path(pid, 1, 2)
    assert ch1.exists() and ch2.exists()
    assert "初入宗门" in ch1.read_text(encoding="utf-8")
    gist = ws.outline_chapter_path(pid, 1, 1)
    fm = json.loads(gist.read_text(encoding="utf-8").split("---")[1])
    assert fm["done"] is True and "初入宗门" in fm["title"]

    # 归并消歧 + 文风画像 provenance
    bp = Blueprint.load(ws, pid)
    chars = bp.section("characters")
    assert any(c["name"] == "陆沉" and c["role"] == "protagonist" for c in chars)
    assert bp.get_provenance("style.tone")["src"] == "ingested"
    assert bp.get_provenance("style.pov")["src"] == "ingested"

    # 实体 warm-up + worldstate 初始态（pending 结构化）
    assert ws.bible_path(pid, "entity_progress").exists()
    ws_data = ws.read_json(pid, ws.bible_path(pid, "worldstate"))
    pend = (ws_data or {}).get("pending") or []
    assert any(p["what"] == "三日后闭关" and p["status"] == "scheduled"
               and p["due"] == 3 for p in pend)

    # stage + transcript
    assert ForgeState.load(ws, pid).stage == "ingested"
    events = [e["event"] for e in read_transcript(ws, pid)]
    assert events == ["ingest.start", "ingest.slice", "ingest.extract",
                      "ingest.blueprint", "ingest.encode", "ingest.end"]


def test_ingest_preserves_chronicler_timeline(ws_factory, tmp_path):
    """LLM 链路：chronicler 逐章推进的 time/pending 不被末尾合成覆盖
    （ADR-019「预计完成时间」语义，用户 2026-09-01 拍板）。

    行格式回复对抽取阶段是无效 JSON → 降级确定性（吃配额但继续）；
    记忆初始化阶段 chronicler 正常解析「时间：/约定：」行。
    """
    ws, pid = ws_factory("proj-f3i")
    src = _write_draft(tmp_path, "第 1 章 甲\n\n陆沉闭关修炼，陆沉心无旁骛，陆沉服下丹药。\n\n"
                                  "第 2 章 乙\n\n陆沉出关，陆沉长啸，声震山谷。\n")
    reply = ("事件：陆沉闭关修炼|discovery|陆沉\n时间：+30日\n约定：三日后出关试炼|+3日\n"
             "事件：陆沉出关|turning_point|陆沉\n时间：+90日\n约定：三日后出关试炼|+3日\n")
    res = run_ingest(ws, pid, str(src), provider=LineProvider(reply),
                     chapters_per_volume=20, target_words=100,
                     mode="auto", io=FakeIO(is_tty=False))
    assert res.ok
    ws_data = ws.read_json(pid, ws.bible_path(pid, "worldstate"))
    assert ws_data["time"]["now"] == 60           # 30 + 30 逐章推进（同一回复 ×2 章），不被覆盖为 0
    pend = ws_data["pending"]
    dues = sorted(p["due"] for p in pend if "出关试炼" in p["what"])
    assert 3 in dues                              # ch1 约定：due = 0 + 3（登记先于推进）
    assert 33 in dues                             # ch2 约定：due = 30 + 3（按当时 day 锚定）
    assert ws_data["characters"]                  # 蓝图人物初始态仍在


def test_ingest_dry_run_no_write(ws_factory, tmp_path):
    ws, pid = ws_factory("proj-f3e")
    src = _write_draft(tmp_path, DRAFT)
    res = run_ingest(ws, pid, str(src), provider=None, target_words=100,
                     dry_run=True, io=FakeIO(is_tty=False))
    assert res.ok and res.chapters_ingested == 2
    assert not ws.chapter_path(pid, 1, 1).exists()
    assert any("dry-run" in w for w in res.warnings)


def test_ingest_interactive_no_provider_skip_consult(ws_factory, tmp_path):
    ws, pid = ws_factory("proj-f3f")
    src = _write_draft(tmp_path, DRAFT)
    res = run_ingest(ws, pid, str(src), provider=None, target_words=100,
                     mode="interactive", io=FakeIO(is_tty=False))
    assert res.ok
    assert any("回落商讨跳过" in w for w in res.warnings)


# ============================================================ 7. 记忆初始化（行格式）


def test_init_memory_writes_events(ws_factory, tmp_path):
    ws, pid = ws_factory("proj-f3g")
    cs = _chunks(("第 1 章", "陆沉服用洗髓丹，突破练气三层。三日后闭关。"))
    chron_reply = ("事件：陆沉服用洗髓丹突破练气三层|discovery|陆沉\n"
                   "事件：陆沉决定闭关冲击筑基|turning_point|陆沉\n"
                   "约定：三日后闭关|+3日\n")
    used, warns = init_memory(ws, pid, cs, LineProvider(chron_reply),
                              chapters_per_volume=20, ingest_max_calls=30, calls_used=0)
    assert used == 1
    assert warns == []
    events = ws.read_json(pid, ws._abs(f"{pid}/memory/plot_events.json"))  # noqa: SLF001
    assert isinstance(events, list) and len(events) >= 1
    assert events[0]["type"] in ("discovery", "turning_point")  # commit 字段是 type
    assert ws._abs(f"{pid}/memory/fragment_index.json").exists()  # RAG 索引重建


def test_init_memory_no_provider_skips(ws_factory, tmp_path):
    ws, pid = ws_factory("proj-f3h")
    cs = _chunks(("第 1 章", "正文"))
    used, warns = init_memory(ws, pid, cs, None,
                              chapters_per_volume=20, ingest_max_calls=30, calls_used=0)
    assert used == 0
    assert any("无 LLM provider" in w for w in warns)


# ============================================================ 8. CLI


def test_cli_ingest_fake_pure_deterministic(ws_factory, tmp_path, monkeypatch):
    from click.testing import CliRunner
    from novelist.cli import cli

    ws, pid = ws_factory("proj-f3i")
    src = _write_draft(tmp_path, DRAFT)
    runner = CliRunner()
    monkeypatch.chdir(str(ws._abs("")))  # noqa: SLF001
    result = runner.invoke(cli, ["forge", "ingest", str(src), "--provider", "fake",
                                 "--target-words", "100"])
    assert result.exit_code == 0, result.output
    assert "ingest done" in result.output
    assert "下一步" in result.output
    assert ws.chapter_path(pid, 1, 1).exists()


def test_cli_ingest_dry_run(ws_factory, tmp_path, monkeypatch):
    from click.testing import CliRunner
    from novelist.cli import cli

    ws, pid = ws_factory("proj-f3j")
    src = _write_draft(tmp_path, DRAFT)
    runner = CliRunner()
    monkeypatch.chdir(str(ws._abs("")))  # noqa: SLF001
    result = runner.invoke(cli, ["forge", "ingest", str(src), "--provider", "fake",
                                 "--target-words", "100", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output
    assert not ws.chapter_path(pid, 1, 1).exists()
