"""P1 常驻会话壳（docs/10 §5.4）单元测试。

覆盖（单测绝不真调 LLM，docs/09 §2.1——FakeProvider/FakeIO）：
- run_shell 会话循环：自由语分派写槽；命令 /show /help /exit /save
- 空回车取推荐值推进
- /build：无缺口直接构建（fake build）；有 required 缺口补推荐值并确认
- 补充设想 → extras 登记
- quit_early：有未收敛缺口时 /exit → stage=consulting
"""

from __future__ import annotations

import pytest

from novelist.forge.state import Blueprint
from novelist.forge.shell import ShellResult, run_shell
from novelist.forge.slots import Slot
from novelist.providers.fake import FakeProvider
from novelist.storage.workspace import Workspace


class FakeIO:
    """会话替身（is_tty=True，输入可脚本化）。"""

    def __init__(self, lines: list[str] | None = None, *, is_tty: bool = True,
                 confirm_default: bool = True) -> None:
        self.lines = list(lines or [])
        self.is_tty = is_tty
        self.out: list[str] = []
        self.confirm_default = confirm_default

    def notify(self, text: str) -> None:
        self.out.append(text)

    def ask_free(self, prompt: str, default: str = "") -> str | None:
        return self.lines.pop(0) if self.lines else "/exit"

    def ask_choice(self, prompt: str, options: list[str], default_idx: int = 0) -> int | None:
        return default_idx

    def confirm(self, prompt: str, default: bool = True) -> bool:
        return self.confirm_default


def _ws(tmp_path: any) -> tuple[Workspace, str]:
    ws = Workspace(root=str(tmp_path))
    pid = "proj-shell"
    ws.create_project(pid)
    return ws, pid


def _blank_bp() -> Blueprint:
    bp = Blueprint.blank()
    bp.data["meta"] = {"title": "t", "genre": "修仙", "logline": "x",
                       "scale": {"volumes": 1, "chapters_per_volume": 1,
                                 "target_words_per_chapter": 100}}
    ws = None
    return bp


def _disp(answers: dict, extras: list[str] | None = None):
    import json

    return json.dumps({"answers": answers, "extras": extras or []})


def test_shell_free_text_writes_slot(tmp_path):
    """自由语 → FakeProvider 返回分派 JSON → 护栏写槽。"""
    ws, pid = _ws(tmp_path)
    bp = _blank_bp()
    slot = Slot("style.tense", "时态", "recommended", "free",
                candidates_from="enum", enum=["过去", "现在"], group=1)
    io = FakeIO(lines=["我选过去时", "/exit"])
    res = run_shell(ws, pid, bp, provider=FakeProvider(reply=_disp({"style.tense": "过去"})),
                    io=io, slots=[slot])
    assert res.ok and res.answered == 1
    assert bp.get("style.tense") == "过去"


def test_shell_extras_recorded(tmp_path):
    """自由语里落不到槽的设想 → extras 登记。"""
    from novelist.forge.ask import _read_extras

    ws, pid = _ws(tmp_path)
    bp = _blank_bp()
    slot = Slot("style.tense", "时态", "recommended", "free",
                candidates_from="enum", enum=["过去", "现在"], group=1)
    io = FakeIO(lines=["补个设想：女主有隐藏身世", "/exit"])
    res = run_shell(ws, pid, bp, io=io,
                    provider=FakeProvider(reply=_disp({}, ["女主有隐藏身世"])),
                    slots=[slot])
    assert res.extras_seen == 1
    extras = _read_extras(ws, pid)
    assert len(extras) == 1 and extras[0]["text"] == "女主有隐藏身世"
    assert extras[0]["status"] == "pending"


def test_shell_empty_enter_fills_recommended(tmp_path):
    """空回车 → 本轮缺口取推荐值推进。"""
    ws, pid = _ws(tmp_path)
    bp = _blank_bp()
    slot = Slot("style.tense", "时态", "recommended", "free",
                candidates_from="enum", enum=["过去", "现在"], default="过去", group=1)
    io = FakeIO(lines=["", "/exit"])
    res = run_shell(ws, pid, bp, io=io, provider=FakeProvider(),
                    slots=[slot])
    assert res.answered == 1
    assert bp.get("style.tense") == "过去"


def test_shell_show_help_are_noop(tmp_path):
    """/show /help 不推进缺口，字段不被清空。"""
    ws, pid = _ws(tmp_path)
    bp = _blank_bp()
    slot = Slot("style.tense", "时态", "recommended", "free",
                candidates_from="enum", enum=["过去", "现在"], group=1)
    io = FakeIO(lines=["/show", "/help", "/exit"])
    res = run_shell(ws, pid, bp, io=io, provider=FakeProvider(), slots=[slot])
    assert res.ok and res.answered == 0
    assert any("已填充" in o or "命令" in o for o in io.out)


def test_shell_exit_keeps_gap_consulting(tmp_path):
    """仍有缺口时 /exit → quit_early=True，stage 保持 consulting。"""
    from novelist.forge import ForgeState

    ws, pid = _ws(tmp_path)
    bp = _blank_bp()
    slot = Slot("style.tense", "时态", "recommended", "free",
                candidates_from="enum", enum=["过去", "现在"], group=1)
    bp.save(ws, pid)
    ForgeState.load(ws, pid).touch_stage(ws, pid, "consulting")
    io = FakeIO(lines=["/exit"])
    res = run_shell(ws, pid, bp, io=io, provider=FakeProvider(), slots=[slot])
    assert res.quit_early is True
    state = ForgeState.load(ws, pid)
    assert state.stage == "consulting"
    assert res.warnings and "缺口" in res.warnings[0]


def test_shell_build_triggered_when_no_gap(tmp_path):
    """无缺口时 /build：confirm 通过 → 触发构建（fake build 不真调 LLM）。"""
    ws, pid = _ws(tmp_path)
    bp = _blank_bp()
    slot = Slot("style.tense", "时态", "recommended", "free",
                candidates_from="enum", enum=["过去", "现在"], default="过去", group=1)
    bp.save(ws, pid)
    # 用非 required 槽且填上值，模拟无 required 缺口
    bp.set("style", {"tense": "过去"})
    bp.save(ws, pid)
    io = FakeIO(lines=["/build", "/exit"])
    res = run_shell(ws, pid, bp, io=io, provider=FakeProvider(), slots=[slot],
                    max_calls=1)
    assert res.build_triggered is True


def test_shell_build_requires_confirm_when_gap(tmp_path):
    """有 required 缺口时 /build：confirma false → 取消构建，回到会话。"""
    ws, pid = _ws(tmp_path)
    bp = _blank_bp()
    slot = Slot("meta.pace", "节奏", "required", "choice",
                candidates_from="enum", enum=["平推爽文", "稳健推进"], group=1)
    io = FakeIO(lines=["/build", "/exit"], confirm_default=False)
    res = run_shell(ws, pid, bp, io=io, provider=FakeProvider(), slots=[slot])
    assert res.build_triggered is False
    assert bp.get("meta.pace") is None  # 未强写


# ---------- 方案2：空回车跳过无默认槽 / 反复跳过系统自动设定待审核 ----------

def test_settle_round_skips_unfillable_first_then_autosets(tmp_path):
    """无默认槽：首次空回车跳过不写；再次空回车触发系统自动设定为低置信待审核。"""
    from novelist.forge.ask import RoundQuestion, _settle_round
    from novelist.forge.state import read_transcript

    ws, pid = _ws(tmp_path)
    bp = _blank_bp()
    # 无默认、enum 槽：_default_of 得空 → 首轮跳过
    slot = Slot("style.tense", "时态", "recommended", "free",
                candidates_from="enum", enum=["过去", "现在"], group=1)
    q = RoundQuestion(slot, ["过去", "现在"], "")
    attempts: dict[str, int] = {}
    answered: set[str] = set()

    rounds = 0
    provider = FakeProvider(reply='{"style.tense": ["现在"]}')
    # 连续多次空回车，必须有界收敛，绝不无限循环
    for _ in range(80):
        _settle_round(bp, [q], provider, FakeIO(), ws, pid,
                      _new_consult_result(), answered, attempts, rounds + 1,
                      "ask.default")
        if q.slot.key in answered:
            break
        rounds += 1
    assert q.slot.key in answered  # 已收敛（系统自动设定）
    prov = bp.get_provenance("style.tense")
    assert prov is not None
    assert prov["src"] == "llm" and prov["confidence"] < 1.0  # 低置信待审核
    assert bp.get("style.tense") == "现在"
    # 系统自动设定留有 ask.auto 待审核事件
    evs = [t["event"] for t in read_transcript(ws, pid)]
    assert "ask.auto" in evs


def test_settle_round_rejects_bad_enum_never_writes(tmp_path):
    """enum 槽非法值：_dispatch_value 拒答，不写 blueprint（回归 schema 崩防护）。"""
    from novelist.forge.ask import _dispatch_value, RoundQuestion

    ws, pid = _ws(tmp_path)
    bp = _blank_bp()
    slot = Slot("style.tense", "时态", "recommended", "free",
                candidates_from="enum", enum=["过去", "现在"], group=1)
    q = RoundQuestion(slot, ["过去", "现在"], "过去")
    # 直接护栏：非候选/越界 → None；合法 → 值
    assert _dispatch_value(q, "9") is None
    assert _dispatch_value(q, "重口味") is None
    assert _dispatch_value(q, "过去") == "过去"


def _new_consult_result():
    from novelist.forge.ask import ConsultResult

    return ConsultResult(ok=True)