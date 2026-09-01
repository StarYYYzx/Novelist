"""M3l F2 测试（docs/10 §13 / docs/08 F2）：商讨问答协议。

覆盖（**单测绝不真调 LLM**，docs/09 §2.1——FakeProvider/StubLLM/FakeIO）：
- parse_round_line：空行/q/编号/自由答案四种回答解析（纯函数）
- ConsoleIO：ask_choice/ask_free/confirm 原语（注入 StringIO）
- run_consult：非 TTY 降级全取推荐值 + AG3 收敛（问数 ≤12）/
  TTY 自由答案 src=user conf=1.0 / 候选 LLM 每轮 1 次调用 / 结构化写入
  （threads/glossary/characters[role:rival].name）/ resume 幂等 / q 提前退出
- seed 集成：授权询问 [2] → run_consult；quit_early 不构建、stage=consulting
- CLI：forge resume 按 stage 分流（consulting → 续问答）
"""

from __future__ import annotations

import json

import pytest

from novelist.forge import Blueprint, ForgeState, run_consult, run_seed
from novelist.forge.ask import parse_round_line
from novelist.forge.io_console import ConsoleIO
from novelist.forge.slots import Slot, default_slots, detect_gaps
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


def _blank_bp() -> Blueprint:
    bp = Blueprint.blank()
    bp.data["meta"] = {"title": "t", "genre": "修仙", "logline": "x",
                       "scale": {"volumes": 1, "chapters_per_volume": 1,
                                 "target_words_per_chapter": 100}}
    return bp


# ---------- parse_round_line（纯函数） ----------

def test_parse_enter_takes_all_defaults():
    qs = [RoundQuestionOf("meta.genre", ["修仙", "都市"], "修仙"),
          RoundQuestionOf("worldview.name", ["落霞界"], "落霞界")]
    ans = parse_round_line("", qs)
    assert ans.quit is False
    assert ans.values["meta.genre"].value == "修仙"
    assert ans.values["meta.genre"].src == "template"
    assert ans.values["meta.genre"].explicit is False


def test_parse_number_selects_candidate():
    qs = [RoundQuestionOf("meta.genre", ["修仙", "都市"], "修仙"),
          RoundQuestionOf("worldview.name", ["落霞界"], "落霞界")]
    ans = parse_round_line("2", qs)
    assert ans.values["worldview.name"].value == "落霞界"
    assert ans.values["worldview.name"].explicit is True
    assert ans.values["worldview.name"].src == "user"


def test_parse_free_answer():
    qs = [RoundQuestionOf("meta.genre", ["修仙"], "修仙")]
    ans = parse_round_line("1 东方蒸汽朋克", qs)
    assert ans.values["meta.genre"].value == "东方蒸汽朋克"
    assert ans.values["meta.genre"].explicit is True
    assert ans.values["meta.genre"].src == "user"


def test_parse_quit():
    qs = [RoundQuestionOf("meta.genre", ["修仙"], "修仙")]
    assert parse_round_line("q", qs).quit is True
    assert parse_round_line("Q", qs).quit is True


def test_parse_garbage_falls_back_to_defaults():
    qs = [RoundQuestionOf("meta.genre", ["修仙"], "修仙")]
    ans = parse_round_line("随意的话", qs)
    assert ans.quit is False
    assert ans.values["meta.genre"].value == "修仙"  # 保守不误写


def RoundQuestionOf(key: str, cands: list[str], default: str):
    from novelist.forge.ask import RoundQuestion

    return RoundQuestion(Slot(key, key), cands, default)


# ---------- ConsoleIO 原语 ----------

def test_console_io_primitives():
    import io as _io

    out = _io.StringIO()
    inp = _io.StringIO("")  # 空输入 → 回车取默认
    cio = ConsoleIO(_in=inp, _out=out)
    assert cio.ask_choice("类型？", ["修仙", "都市"]) == 0  # 推荐
    inp = _io.StringIO("2\n")
    cio = ConsoleIO(_in=inp, _out=out)
    assert cio.ask_choice("类型？", ["修仙", "都市"]) == 1
    inp = _io.StringIO("q\n")
    cio = ConsoleIO(_in=inp, _out=out)
    assert cio.ask_choice("类型？", ["修仙", "都市"]) is None
    inp = _io.StringIO("\n")
    cio = ConsoleIO(_in=inp, _out=out)
    assert cio.ask_free("世界名？", "落霞界") == "落霞界"
    inp = _io.StringIO("东方蒸汽朋克\n")
    cio = ConsoleIO(_in=inp, _out=out)
    assert cio.ask_free("类型？") == "东方蒸汽朋克"
    inp = _io.StringIO("y\n")
    cio = ConsoleIO(_in=inp, _out=out)
    assert cio.confirm("继续？") is True


# ---------- run_consult：非 TTY 降级 + AG3 收敛 ----------

def test_consult_notty_downgrades_all_defaults(ws_factory):
    """非 TTY：interactive 全取推荐值、写 transcript、问数收敛（AG3 ≤12 问）。"""
    ws, pid = ws_factory("proj-f2a")
    bp = _blank_bp()
    consult = run_consult(ws, pid, bp, provider=FakeProvider(reply='{"x": ["a"]}'),
                          io=FakeIO(is_tty=False))
    assert consult.downgraded is True
    assert consult.answered > 0
    assert consult.answered <= 12  # AG3：12 问内收敛
    assert consult.rounds_done >= 1
    events = read_transcript(ws, pid)
    assert any(e["event"] == "round.downgrade" for e in events)
    # 被问过且有推荐值的槽位已填；空推荐值（如主角名）保持缺口；meta.scale 结构未被破坏
    gaps = detect_gaps(bp)
    assert len(gaps) < len(default_slots())
    assert isinstance(bp.get("meta.scale"), (dict, type(None)))


# ---------- run_consult：TTY 自由答案 / 候选 / 结构化 / resume / q ----------

def test_consult_tty_free_answer_sets_user_provenance(ws_factory):
    ws, pid = ws_factory("proj-f2b")
    bp = _blank_bp()
    consult = run_consult(
        ws, pid, bp, provider=FakeProvider(reply='{"worldview.name": ["落霞界"]}'),
        io=FakeIO(lines=["1 东方蒸汽朋克"]),
        slots=[_slot("worldview.name", "llm", 2), _slot("style.tense", "enum", 3, enum=["过去", "现在"])],
    )
    assert consult.free_answers == 1
    assert bp.get("worldview.name") == "东方蒸汽朋克"
    prov = bp.get_provenance("worldview.name")
    assert prov["src"] == "user" and prov["confidence"] == 1.0
    assert bp.get("style.tense") == "过去"  # 回车默认


def test_consult_candidates_one_llm_call_per_round(ws_factory, stub_llm):
    """候选由 LLM 批量生成：每轮 1 次调用（不是每题一次）。"""
    ws, pid = ws_factory("proj-f2c")
    bp = _blank_bp()
    llm = stub_llm(json.dumps({"style.glossary": ["灵气", "灵根"], "worldview.name": ["落霞界", "玄天域"]},
                              ensure_ascii=False))
    consult = run_consult(
        ws, pid, bp, provider=llm, io=FakeIO(lines=["", ""]),
        slots=[_slot("worldview.name", "llm", 2), _slot("style.glossary", "llm", 3)],
    )
    assert consult.rounds_done == 2
    assert len(llm.calls) == 2  # 每轮 1 次
    # 候选 prompt 带蓝图上下文（已有设定摘要）
    assert "本书已有设定" in llm.calls[0]
    # 回车默认取 LLM 候选第一个（src=llm）
    assert bp.get("worldview.name") == "落霞界"
    assert bp.get_provenance("worldview.name")["src"] == "llm"


def test_consult_candidates_failure_falls_back(ws_factory):
    ws, pid = ws_factory("proj-f2d")
    bp = _blank_bp()
    consult = run_consult(
        ws, pid, bp, provider=FakeProvider(reply="不是 JSON"),
        io=FakeIO(lines=[""]),
        slots=[_slot("worldview.name", "llm", 2)],
    )
    assert consult.answered == 0  # 无默认值（蓝图空）→ 空值跳过，不阻断
    assert any("候选生成失败" in w for w in consult.warnings)
    assert any(e["event"] == "candidates.fallback" for e in read_transcript(ws, pid))


def test_consult_structured_targets(ws_factory):
    """threads/glossary/characters[role:rival].name 按结构写入。"""
    ws, pid = ws_factory("proj-f2e")
    bp = _blank_bp()
    consult = run_consult(
        ws, pid, bp, provider=FakeProvider(reply="{}"),
        io=FakeIO(lines=["1 灵气、灵根、神识", "1 云纹玉牌之谜", "1 血魔老祖"]),
        slots=[_slot("threads", "llm", 4), _slot("style.glossary", "llm", 3),
               _slot("characters[role:rival].name", "llm", 5)],
    )
    assert consult.free_answers == 3
    threads = bp.section("threads")
    assert len(threads) == 1 and threads[0]["desc"] == "云纹玉牌之谜"
    assert threads[0]["id"].startswith("pt:")
    gloss = bp.data["style"]["glossary"]  # 嵌套路径不可用 section（只认顶层数组）
    assert len(gloss) == 1 and gloss[0]["term"] == "灵气、灵根、神识"
    rival = next(c for c in bp.section("characters") if c.get("role") == "rival")
    assert rival["name"] == "血魔老祖"
    assert bp.get_provenance(f"characters[{rival['id']}].name")["src"] == "user"


def test_consult_resume_skips_answered(ws_factory):
    """resume 幂等：已答槽位自动跳过（transcript 判据）；空推荐值槽位保持缺口下次再问。"""
    ws, pid = ws_factory("proj-f2f")
    bp = _blank_bp()
    slots = [_slot("worldview.name", "llm", 2), _slot("worldview.rules", "llm", 2),
             _slot("style.tense", "enum", 3, enum=["过去", "现在"])]
    r1 = run_consult(ws, pid, bp, provider=FakeProvider(reply='{"worldview.name": ["落霞界"]}'),
                     io=FakeIO(lines=["1 玄天域"]), slots=slots)
    assert r1.answered == 2  # 轮 2：name 自由答案 + rules 空推荐跳过；轮 3：tense 默认
    assert r1.rounds_done == 2
    assert bp.get("worldview.name") == "玄天域"
    # 第二次：name 已答 → 轮 2 只剩 rules（再问）；tense 已答 → 轮 3 跳过
    r2 = run_consult(ws, pid, bp, provider=FakeProvider(reply="{}"),
                     io=FakeIO(lines=["1 云纹令只认陆氏血脉"]), slots=slots)
    assert r2.answered == 1  # rules（自由）
    assert r2.rounds_done == 1
    assert bp.get("worldview.rules") == ["云纹令只认陆氏血脉"]
    assert bp.get("style.tense") == "过去"
    # name 没有被重复问（transcript 里只答了一次）
    answers = [e for e in read_transcript(ws, pid) if e["event"] == "ask.answer"
               and e["key"] == "worldview.name"]
    assert len(answers) == 1


def test_consult_quit_stops_later_rounds(ws_factory):
    ws, pid = ws_factory("proj-f2g")
    bp = _blank_bp()
    consult = run_consult(
        ws, pid, bp, provider=FakeProvider(reply="{}"),
        io=FakeIO(lines=["q"]),
        slots=[_slot("worldview.name", "llm", 2), _slot("style.tense", "enum", 3, enum=["过去", "现在"])],
    )
    assert consult.quit_early is True
    assert consult.rounds_done == 1  # 只跑了第 2 轮即退出（第 3 轮不再问）
    assert any(e["event"] == "round.quit" for e in read_transcript(ws, pid))
    assert bp.get("style.tense") is None  # 后续轮未执行


# ---------- seed 集成：授权 [2] 商讨 ----------

class FakeTTY:
    """isatty()=True 的 stdin/stdout 替身；readline 按脚本返回，write 丢弃。"""

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)
        self._buf = ""

    def isatty(self) -> bool:
        return True

    def readline(self) -> str:
        return self._lines.pop(0) if self._lines else ""

    def write(self, s: str) -> int:
        self._buf += s
        return len(s)

    def flush(self) -> None:
        pass


def test_seed_consult_quit_early_no_build(ws_factory, monkeypatch):
    """授权 [2] 商讨 + 立即 q：不构建，stage=consulting，quit_early 上报。"""
    ws, pid = ws_factory("proj-f2h")
    monkeypatch.setattr("sys.stdin", FakeTTY(["2\n", "q\n"]))
    monkeypatch.setattr("sys.stdout", FakeTTY([]))
    res = run_seed(ws, pid, "叶蓝绑定五五开系统", provider=FakeProvider(reply='{"genre": "修仙"}'),
                   mode="interactive", volumes=1, chapters_per_volume=1, target_words=100,
                   max_calls=60)
    assert res.quit_early is True
    assert res.build.get("ok") is False  # 未构建
    state = ForgeState.load(ws, pid)
    assert state.stage == "consulting"
    assert any(e["event"] == "round.quit" for e in read_transcript(ws, pid))


def test_seed_consult_completes_then_builds(ws_factory, monkeypatch):
    """商讨完整跑完（回车全默认）→ 继续构建（脚本化 LLM 驱动）。"""
    from novelist.core.llm import LLMResult

    ws, pid = ws_factory("proj-f2i")
    # 授权选 2 → 每轮回车取默认 → 全部轮次跑完 → 构建
    tty = FakeTTY(["2\n"] + [""] * 8)
    monkeypatch.setattr("sys.stdin", tty)
    monkeypatch.setattr("sys.stdout", FakeTTY([]))
    # 脚本化回复：seed 提炼(1) + 4 轮候选 + book + volume + chapter = 9 次调用
    book_reply = json.dumps({
        "artifact": {"worldview": {"name": "落霞界"},
                     "characters": [{"id": "char:yelan", "name": "叶蓝", "role": "protagonist"}],
                     "volumes": [{"vol": 1, "title": "初入修行", "summary": "觉醒"}]},
        "decide": "done", "reason": "r"}, ensure_ascii=False)
    vol_reply = json.dumps({"artifact": {"vol": 1, "title": "初入修行", "summary": "觉醒",
                                         "key_beats": ["觉醒"]}, "decide": "done", "reason": "r"},
                           ensure_ascii=False)
    ch_reply = json.dumps({"artifact": {"title": "章 1", "pov": "第三人称限知",
                                        "key_events": ["事件1"], "turns": [], "after_days": 0},
                           "decide": "done", "reason": "r"}, ensure_ascii=False)
    replies = [
        json.dumps({"genre": "修仙", "template_suggestion": "修仙男频", "logline": "五五开",
                    "protagonist_hint": {"name": "叶蓝", "gender": "male", "cheat": "五五开系统"},
                    "scale_hint": {"volumes": 1, "chapters_per_volume": 1},
                    "time_origin": "穿越", "unknowns": []}, ensure_ascii=False),
        '{"meta.genre": ["修仙"]}',
        '{"worldview.name": ["落霞界"]}',
        '{"style.tone": ["热血激昂"]}',
        '{"threads": ["云纹玉牌之谜"]}',
        book_reply, vol_reply, ch_reply,
    ]

    class ScriptLLM:
        def __init__(self, texts):
            self.texts = list(texts)
            self.calls = 0

        def complete(self, req):
            self.calls += 1
            text = self.texts.pop(0) if self.texts else '{"decide": "done"}'
            return LLMResult(ok=True, content=text, finish_reason="stop", blocked=False)

    res = run_seed(ws, pid, "叶蓝绑定五五开系统", provider=ScriptLLM(replies),
                   mode="interactive", volumes=1, chapters_per_volume=1, target_words=100,
                   max_calls=60)
    assert res.quit_early is False
    assert res.ok is True
    assert res.build.get("ok") is True
    assert res.build.get("volumes_written") == 1
    state = ForgeState.load(ws, pid)
    assert state.stage == "built"


# ---------- CLI：forge resume 分流 ----------

def test_cli_resume_consulting_branch(ws_factory, monkeypatch):
    """stage=consulting 时 forge resume 续商讨（非 TTY 自动取推荐值）。"""
    from click.testing import CliRunner
    from novelist.cli import cli

    ws, pid = ws_factory("proj-f2j")
    bp = _blank_bp()
    bp.save(ws, pid)
    state = ForgeState.load(ws, pid)
    state.stage = "consulting"
    state.save(ws, pid)
    runner = CliRunner()
    monkeypatch.chdir(str(ws._abs("")))  # noqa: SLF001
    result = runner.invoke(cli, ["forge", "resume", pid, "--provider", "fake"])
    assert result.exit_code == 0, result.output
    assert "consult resume done" in result.output
    state2 = ForgeState.load(ws, pid)
    assert state2.stage == "seeded"  # 商讨完成（非 TTY 全默认，无 quit）


def test_cli_resume_build_branch(ws_factory, monkeypatch):
    """stage != consulting 时 forge resume 走构建续跑（脚本驱动，幂等）。"""
    from click.testing import CliRunner
    from novelist.cli import cli
    from novelist.providers.fake import ScriptedProvider

    ws, pid = ws_factory("proj-f2k")
    bp = _blank_bp()
    bp.save(ws, pid)
    # 蓝图已存、stage 默认（非 consulting）→ 构建续跑：book→vol→chapter 脚本
    book = json.dumps({"artifact": {"worldview": {"name": "落霞界"},
                                    "volumes": [{"vol": 1, "title": "卷一", "summary": "s"}]},
                       "decide": "done", "reason": "r"}, ensure_ascii=False)
    vol = json.dumps({"artifact": {"vol": 1, "title": "卷一", "summary": "s", "key_beats": ["k"]},
                      "decide": "done", "reason": "r"}, ensure_ascii=False)
    ch = json.dumps({"artifact": {"title": "章1", "pov": "第三人称限知", "key_events": ["e"],
                                  "turns": [], "after_days": 0},
                     "decide": "done", "reason": "r"}, ensure_ascii=False)
    monkeypatch.setattr("novelist.cli._make_cli_provider",
                        lambda p: ScriptedProvider([{"final": book}, {"final": vol}, {"final": ch}]))
    runner = CliRunner()
    monkeypatch.chdir(str(ws._abs("")))  # noqa: SLF001
    result = runner.invoke(cli, ["forge", "resume", pid, "--provider", "fake"])
    assert result.exit_code == 0, result.output
    assert "resume done" in result.output
    assert ws.outline_chapter_path(pid, 1, 1).exists()


def _slot(key: str, candidates_from: str, group: int, *, enum: list[str] | None = None,
          default: str = "") -> Slot:
    return Slot(key, key, "recommended", "free", ask=f"{key}？",
                candidates_from=candidates_from, enum=enum or [], default=default, group=group)
