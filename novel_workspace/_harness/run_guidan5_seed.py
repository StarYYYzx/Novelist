"""规则怪谈新项目五章链路 · 阶段 1：seed + 商讨 + 构建（服务器 C 35B）。

复制 run_cloudb5_seed.py 骨架，差异：
- BRIEF = 用户一句话需求 + 四项细化拍板（死亡+穿越双场 / 恐怖+爽感并重 /
  房子→图书馆→学校 / 玩家视角穿插）
- 隧道 127.0.0.1:18006 → C:6006（FreeToken Qwen3.6-35B-A3B-FP8，ctx 16384）
- PID 前缀 proj-guidan5-*

用法：python run_guidan5_seed.py
产出：proj-guidan5-*/workspace/forge/blueprint.json + bible/* + outline/（细纲 1-1..1-5）
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
    "规则怪谈流无限流：男主陈讯（男，21岁）原是蓝星普通大学生，因意外身亡，"
    "身亡后穿越到一个怪谈降临的世界，成为了一名规则怪谈管理员，可以创造规则怪谈副本，"
    "并随机拉人进入。最初男主只能创建一个房子大小、内含一个低等诡异的副本，"
    "但随着男主不断创建规则怪谈，他能创建的副本规模也越来越大"
    "（房子→图书馆→学校→体育场→村庄……）。当男主完成一定指标后"
    "（例如副本累计制裁了多少违规者、总共创建了多少个副本），"
    "就可以打开自己创造的世界与现实世界的大门，将管理员能力作用到现实世界。"
    "这个世界名为蓝星，怪谈降临，每隔一段时间就会随机拉人作为玩家进入怪谈副本，"
    "副本内玩家全灭则诡异就会出现在现实世界造成损伤。男主最终目的是拯救世界："
    "他只拉罪大恶极的人物进入自己设计的副本，借诡异之手制裁恶人，"
    "在达到最终目的后让世界不再被诡异侵扰。副本中有两种实体："
    "1.NPC，和正常人类一样，负责推动剧情、发布任务，或单纯充实副本；"
    "2.诡异，遵循一定的规则，可以引发一定的超自然现象，会制裁违反副本规则的玩家。"
    "细化拍板：①第 1 章从现实死亡瞬间写起，再落地怪谈世界觉醒能力（死亡+穿越双场）；"
    "②文风怪谈恐怖与掌控爽感并重，诡异描写有压迫感、规则细思极恐，"
    "但主角全局掌控的爽感做主调；③五章内副本规模走三段成长："
    "房子（首杀）→图书馆→学校规模并预告双门指标；"
    "④每章穿插至少一段罪人玩家进副本的视角，写其从嚣张到崩溃被诡异制裁的全过程。"
)
PID = "proj-guidan5-" + time.strftime("%Y%m%d-%H%M%S")
LOG = Path(__file__).resolve().parent / "guidan5_seed_log.txt"

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
    """商讨问答的脚本化终端（与 run_cloudb5_seed.py 相同策略）。"""

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
                   genre_pack="通用", volumes=1, chapters_per_volume=5,
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

    bres = build(ws, PID, provider=provider, max_calls=60, resume=False, deepen=True,
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
