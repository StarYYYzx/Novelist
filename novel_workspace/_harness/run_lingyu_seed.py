"""无敌领域长篇项目五章链路 · 阶段 1：seed + 商讨 + 构建（服务器 C 35B）。

复制 run_guidan5_seed.py 骨架，差异：
- BRIEF = 一句话需求 + 五项细化拍板（倒叙开场 / 爽感+反差幽默 /
  长篇不封顶只写第1卷前5章 / 极慢热院子→出院门 / 主角线+宗门暗线双线）
- **长篇口径**：volumes=3, chapters_per_volume=8 —— 卷纲 3 卷一次出齐，
  细纲仅 vol1（8 章），正文只写 1-1..1-5（engine.py: vol!=1 不展开章）
- PID 前缀 proj-lingyu5-*
- 隧道 127.0.0.1:18006 → C:6006（FreeToken Qwen3.6-35B-A3B-FP8，ctx 16384）

用法：C:/Python314/python.exe run_lingyu_seed.py
产出：proj-lingyu5-*/workspace/forge/blueprint.json + bible/* + outline/
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "novel_workspace" / "_harness"))

from novelist.forge import run_seed
from novelist.forge.ask import run_consult
from novelist.forge.slots import slots_for_genre
from novelist.providers.lmstudio import LMStudioProvider
from novelist.storage.workspace import Workspace

BRIEF = (
    "东方修仙长篇无敌流：男主江浩（男，20 岁）穿越到修仙世界，"
    "觉醒了无敌领域系统——在领域范围内他绝对无敌，任何修为高于他的存在也无法伤他分毫，"
    "但他自身的修为与战力仍在领域外遵循常规。初始领域范围只有他家的院子那么大。"
    "系统会发布任务，完成任务可获得扩大领域半径的奖励。"
    "男主最终通过不断完成任务，将领域从院子一路扩展，直至覆盖整个世界。"
    "重要口径：本书是长篇小说，篇幅不封顶，'领域覆盖整个世界'是全书远期主线结局，"
    "绝不允许压缩进本次规划的前几卷；本次只规划前 3 卷，并撰写第 1 卷的前 5 章。"
    "细化拍板：①第 1 章倒叙开场：从多年后江湖流传的'有一人立于其所立之处，"
    "天下无可伤其分毫'的传说画面切入，再闪回至江浩初落修仙世界、"
    "在自家院内首次觉醒系统的起点，末尾留下系统首个任务；"
    "②文风爽感为主+反差幽默：领域内众生平等、领域外苟着发育，"
    "敌人跨入院子的瞬间画风突变，无敌流经典配方，幽默感来自反差而非恶搞；"
    "③第 1 卷弧线极慢热：前 5 章只写到江浩第一次跨出院门（领域从院子扩展到"
    "出院门级别，约半亩地），重心放在系统任务机制、江浩与邻里村镇的人物关系、"
    "修仙世界底层生态的铺陈，不写宗门大 conflict；"
    "④双线结构：主线贴江浩（系统任务+院内无敌日常），"
    "暗线为远处宗门对这偏远乡镇异常气象的察觉（为后续卷冲突埋线），"
    "暗线以章末或章节间短场景呈现，每章至多一段；"
    "⑤修仙世界观按传统境界体系（炼气/筑基/金丹……），江浩本人初期只是毫无修为的凡人，"
    "无敌性完全来自领域而非自身修为，且系统任务优先引导他在凡俗层面解决问题。"
)
PID = "proj-lingyu5-" + time.strftime("%Y%m%d-%H%M%S")
LOG = Path(__file__).resolve().parent / "lingyu5_seed_log.txt"

MODEL = "qwen3.6-35b-a3b-fp8"


def make_provider() -> LMStudioProvider:
    return LMStudioProvider(
        base_url="http://127.0.0.1:18006",
        model=MODEL,
        enable_thinking=False,   # llama.cpp：chat_template_kwargs 才有效
        reasoning_aware=True,
        timeout_s=1800,
    )


class ScriptedConsult:
    """商讨问答的脚本化终端（与 run_guidan5_seed.py 相同策略）。"""

    def __init__(self, log_path: Path) -> None:
        self._log = open(log_path, "a", encoding="utf-8")
        self._round_no = 0
        self._free_used = False

    @property
    def is_tty(self) -> bool:
        return True

    def notify(self, text: str) -> None:
        if "第" in text and "轮" in text and "题" in text:
            self._round_no += 1
        print(text, flush=True)
        self._log.write(text + "\n")
        self._log.flush()

    def ask_free(self, prompt: str, default: str = "") -> str | None:
        ans = "1" if self._round_no == 1 and not self._free_used else ""
        if ans == "1":
            self._free_used = True
        print(f"  >> [脚本回答] {ans!r}", flush=True)
        self._log.write(f"ANSWER: {ans!r}\n")
        self._log.flush()
        return ans

    def ask_choice(self, prompt: str, options: list[str], default_idx: int = 0) -> int | None:
        return default_idx

    def confirm(self, prompt: str, default: bool = True) -> bool:
        return default


def main() -> None:
    ws = Workspace(root=str(ROOT / "novel_workspace"))
    provider = make_provider()

    print(f"== 项目 {PID} ==", flush=True)
    print("== 阶段 1a：seed 提炼（smoke，不构建）==", flush=True)
    res = run_seed(ws, PID, BRIEF, provider=provider, mode="auto", smoke=True,
                   genre_pack="通用", volumes=3, chapters_per_volume=8,
                   target_words=2400)
    for w in res.warnings:
        print(f"  [warn] {w}", flush=True)
    print(f"seed smoke ok={res.ok} spec.template={res.spec.template_suggestion if res.spec else '-'}",
          flush=True)

    print("\n== 阶段 1b：商讨问答（世界观追问）==", flush=True)
    from novelist.forge import genres as _genres
    from novelist.forge.io_console import AnswerIO  # noqa: F401
    from novelist.forge.state import Blueprint, ForgeState

    bp = Blueprint.load(ws, PID)
    io = ScriptedConsult(LOG)
    consult = run_consult(ws, PID, bp, provider=provider, io=io,
                          slots=slots_for_genre(_genres.load_pack(_genres.GENERIC_ID)))
    for w in consult.warnings:
        print(f"  [warn] {w}", flush=True)
    print(f"consult: answered={consult.answered} free={consult.free_answers} "
          f"rounds={consult.rounds_done} quit_early={consult.quit_early} "
          f"downgraded={consult.downgraded}", flush=True)

    state = ForgeState.load(ws, PID)
    state.touch_stage(ws, PID, "consulted")
    state.save(ws, PID)

    print("\n== 阶段 1c：全权构建（book→深化→volume→细纲）==", flush=True)
    from novelist.forge import build

    bres = build(ws, PID, provider=provider, max_calls=90, resume=False, deepen=True,
                 coherence_review=True)
    for w in bres.warnings:
        print(f"  [warn] {w}", flush=True)
    print(f"build: calls={bres.calls_used} nodes={bres.nodes_done} "
          f"卷={bres.volumes_written} 章={bres.chapters_written} ok={bres.ok}", flush=True)
    if bres.gate_halted:
        print("审核闸门挂起，待处置:", bres.pending_review, flush=True)
    print(f"\n== 完成：细纲见 novel_workspace/{PID}/outline/chapters/ ==", flush=True)


if __name__ == "__main__":
    main()
