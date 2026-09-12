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


from novelist.forge import Blueprint, ForgeState, run_consult, run_seed
from novelist.forge.io_console import ConsoleIO
from novelist.forge.slots import Slot
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


# ---------- guided：确定性护栏（_dispatch_value / _parse_dispatch）----------

def _enum_round(key: str="style.tense", cands: list[str] | None = None, enum: list[str] | None = None):
    from novelist.forge.ask import RoundQuestion
    cands = cands or ["过去", "现在"]
    slot = Slot(key, key, "recommended", "free", candidates_from="enum",
                enum=enum or cands, group=1)
    return RoundQuestion(slot, cands, cands[0])


def test_dispatch_number_selects_candidate():
    """自由语里用户选序号 → 取对应候选。"""
    from novelist.forge.ask import _dispatch_value
    from novelist.forge.ask import RoundQuestion
    q = RoundQuestion(Slot("meta.genre", "genre", "recommended", "free",
                           candidates_from="llm", group=1),
                      ["修仙", "都市"], "修仙")
    assert _dispatch_value(q, "2") == "都市"
    assert _dispatch_value(q, "1 东方蒸汽朋克") == "1 东方蒸汽朋克"  # 非纯数字自由文本照收


def test_dispatch_enum_out_of_range_and_mismatch_rejected():
    """enum 槽：序号越界或值不在候选 → None（拒答，绝不写进 blueprint 触发 schema 崩）。"""
    from novelist.forge.ask import _dispatch_value
    q = _enum_round()
    assert _dispatch_value(q, "9") is None       # 序号越界
    assert _dispatch_value(q, "被打压的关系户") is None  # 自由语未落在合法选项
    assert _dispatch_value(q, "现在") == "现在"   # 合法值通过


def test_dispatch_enum_cn_label_backmaps_to_id():
    """工艺卡 enum 槽：用户写中文名/id/「中文名 (id)」都能反查回 id（2026-09-10）。"""
    from novelist.forge.ask import _dispatch_value
    from novelist.forge.ask import RoundQuestion
    enum = ["system-flow", "chapter-rhythm"]
    slot = Slot("style.craft_cards", "题材工艺卡", "recommended", "free",
                candidates_from="enum", enum=enum, group=3)
    slot.enum_labels = {"system-flow": "系统流", "chapter-rhythm": "网文章节节奏"}
    q = RoundQuestion(slot, enum, "")
    assert _dispatch_value(q, "系统流") == "system-flow"       # 中文名
    assert _dispatch_value(q, "系统流 (system-flow)") == "system-flow"  # 显示串
    assert _dispatch_value(q, "system-flow") == "system-flow"  # 纯 id
    assert _dispatch_value(q, "被打压的关系户") is None         # 无关词拒答


def test_render_round_shows_cn_labels_not_only_ids():
    """工艺卡候选展示中文名（带 id），不再纯英文（2026-09-10）。"""
    from novelist.forge.ask import _render_round
    from novelist.forge.ask import RoundQuestion
    enum = ["system-flow", "chapter-rhythm", "foreshadowing"]
    slot = Slot("style.craft_cards", "题材工艺卡", "recommended", "free",
                candidates_from="enum", enum=enum, group=3)
    slot.enum_labels = {"system-flow": "系统流", "chapter-rhythm": "网文章节节奏",
                        "foreshadowing": "伏笔与回收"}
    qs = [RoundQuestion(slot, enum, "")]
    txt = _render_round(1, qs)
    assert "系统流 (system-flow)" in txt
    assert "网文章节节奏 (chapter-rhythm)" in txt
    assert "伏笔与回收 (foreshadowing)" in txt


def test_dispatch_parse_dispatch_json():
    """分派产物解析：answers/extras 抽取；畸形输入 → None。"""
    from novelist.forge.ask import _parse_dispatch
    d = _parse_dispatch('{"answers": {"meta.romance": "单女主"}, "extras": ["女主有隐藏身世"]}')
    assert d["answers"]["meta.romance"] == "单女主"
    assert d["extras"] == ["女主有隐藏身世"]
    assert _parse_dispatch("不是 JSON") is None
    assert _parse_dispatch('{"nope": 1}') is None  # 无 answers → 判失败


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


# ---------- run_consult：非 TTY 降级 ----------

def test_consult_notty_downgrades_all_defaults(ws_factory):
    """非 TTY：全取推荐值、写 consult.downgrade、meta.scale 结构不被破坏。"""
    ws, pid = ws_factory("proj-f2a")
    bp = _blank_bp()
    consult = run_consult(ws, pid, bp, provider=FakeProvider(reply='{"x": ["a"]}'),
                          io=FakeIO(is_tty=False))
    assert consult.downgraded is True
    assert consult.answered > 0
    events = read_transcript(ws, pid)
    assert any(e["event"] == "consult.downgrade" for e in events)
    # 有推荐值且非“（留空”的槽位已填；其余保持缺口；
    assert isinstance(bp.get("meta.scale"), (dict, type(None)))


# ---------- run_consult：TTY guided 对话式 ----------

def test_consult_free_answer_through_dispatch(ws_factory):
    """自由语经 LLM 分派落蓝图：明确回答 src=user conf=1.0；未提及槽位留缺口。"""
    ws, pid = ws_factory("proj-f2b")
    bp = _blank_bp()
    consult = run_consult(
        ws, pid, bp,
        provider=FakeProvider(reply='{"answers": {"worldview.name": "东方蒸汽朋克"}}'),
        io=FakeIO(lines=["东方蒸汽朋克"]),
        slots=[_slot("worldview.name", "llm", 2), _slot("style.tense", "enum", 3, enum=["过去", "现在"])],
    )
    assert consult.free_answers == 1
    assert bp.get("worldview.name") == "东方蒸汽朋克"
    prov = bp.get_provenance("worldview.name")
    assert prov["src"] == "user" and prov["confidence"] == 1.0
    # 本轮并未提到 style.tense → 该题未被写入；下一轮用户回车取推荐值
    assert bp.get("style.tense") == "过去"
    assert consult.rounds_done == 2


def test_consult_enter_takes_all_recommended(ws_factory):
    """回车 = 本轮通取推荐值；llm 槽推荐值来自候选 LLM（src=llm）。"""
    ws, pid = ws_factory("proj-f2c")
    bp = _blank_bp()
    consult = run_consult(
        ws, pid, bp, provider=FakeProvider(reply='{"worldview.name": ["落霞界", "玄天域"]}'),
        io=FakeIO(lines=["", ""]),
        slots=[_slot("worldview.name", "llm", 2), _slot("style.tense", "enum", 3, enum=["过去", "现在"])],
    )
    assert consult.rounds_done == 1  # 两槽同在一窗口一次问清
    assert bp.get("worldview.name") == "落霞界"
    assert bp.get_provenance("worldview.name")["src"] == "llm"
    assert bp.get("style.tense") == "过去"


def test_consult_candidates_failure_falls_back(ws_factory):
    """LLM 候选失败 → 该项按默认值兜底，不阻断商讨。"""
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


def test_consult_show_returns_same_round(ws_factory):
    """`?` 打印已填概览后回到同一轮，不推进、不重复问已回答。"""
    ws, pid = ws_factory("proj-f2s")
    bp = _blank_bp()
    consult = run_consult(
        ws, pid, bp,
        provider=FakeProvider(reply='{"answers": {"worldview.name": "玄天域"}}'),
        io=FakeIO(lines=["?", "玄天域"]),
        slots=[_slot("worldview.name", "llm", 2)],
    )
    assert consult.free_answers == 1
    assert bp.get("worldview.name") == "玄天域"
    # 只真正推进了一轮（? 那步未计轮）
    assert consult.rounds_done == 1
    answers = [e for e in read_transcript(ws, pid) if e["event"] == "ask.answer"
               and e["key"] == "worldview.name"]
    assert len(answers) == 1


def test_consult_extras_recorded(ws_factory):
    """无槽可归的补充设想 → extras.json 登记 + extra.idea transcript。"""
    ws, pid = ws_factory("proj-f2x")
    bp = _blank_bp()
    from novelist.forge.ask import _read_extras
    consult = run_consult(
        ws, pid, bp,
        provider=FakeProvider(reply='{"answers": {}, "extras": ["女主有隐藏身世"]}'),
        io=FakeIO(lines=["随便说点设想"]),
        slots=[_slot("worldview.name", "llm", 2)],
    )
    assert consult.extras_seen == 1
    extras = _read_extras(ws, pid)
    assert len(extras) == 1 and extras[0]["text"] == "女主有隐藏身世"
    assert extras[0]["status"] == "pending"
    assert any(e["event"] == "extra.idea" for e in read_transcript(ws, pid))


def test_consult_structured_targets(ws_factory):
    """threads/glossary/characters[role:rival].name 按结构写入。"""
    ws, pid = ws_factory("proj-f2e")
    bp = _blank_bp()
    consult = run_consult(
        ws, pid, bp,
        provider=FakeProvider(reply='{"answers": {"threads": "云纹玉牌之谜", '
                                    '"style.glossary": "灵气、灵根、神识", '
                                    '"characters[role:rival].name": "血魔老祖"}}'),
        io=FakeIO(lines=["三题都答"]),
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
    run_consult(ws, pid, bp,
                     provider=FakeProvider(reply='{"answers": {"worldview.name": "玄天域"}}'),
                     io=FakeIO(lines=["", "玄天域"]), slots=slots)
    # 轮 1 回车：name 无推荐（留空跳过）、rules 无推荐跳过、tense 取推荐
    run_consult(ws, pid, bp,
                     provider=FakeProvider(reply='{"answers": {"worldview.rules": "云纹令只认陆氏血脉"}}'),
                     io=FakeIO(lines=["云纹令只认陆氏血脉"]), slots=slots)
    assert bp.get("worldview.rules") == ["云纹令只认陆氏血脉"]
    assert bp.get("style.tense") == "过去"
    # name 只答过一次，未被 resume 重复问
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
    assert any(e["event"] == "round.quit" for e in read_transcript(ws, pid))


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
                   max_calls=60, gate=False)
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
                   max_calls=60, gate=False)
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
                        lambda p, **kw: ScriptedProvider([{"final": book}, {"final": vol}, {"final": ch}]))
    runner = CliRunner()
    monkeypatch.chdir(str(ws._abs("")))  # noqa: SLF001
    # 本测试验证 resume 分支而非审核闸门：全关开关（ADR-024）
    from novelist.forge.review import REVIEW_MODULES, load_review, save_review

    cfg = load_review(ws, pid)
    cfg["switches"] = {m: False for m in REVIEW_MODULES}
    save_review(ws, pid, cfg)
    result = runner.invoke(cli, ["forge", "resume", pid, "--provider", "fake", "--no-deepen"])
    assert result.exit_code == 0, result.output
    assert "resume done" in result.output
    assert ws.outline_chapter_path(pid, 1, 1).exists()


def _slot(key: str, candidates_from: str, group: int, *, enum: list[str] | None = None,
          default: str = "") -> Slot:
    return Slot(key, key, "recommended", "free", ask=f"{key}？",
                candidates_from=candidates_from, enum=enum or [], default=default, group=group)
