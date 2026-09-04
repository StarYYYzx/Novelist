"""云端新书全链路测试 · 阶段 1：seed 提炼 + 商讨问答（世界观追问）+ 构建。

复刻 proj-20260903194907 的一句话用例与规模（1 卷 × 5 章 × 2400 字，通用包），
走 run_seed(smoke) → run_consult(ScriptedIO，is_tty=True 触发 LLM 候选生成) → build。

用法：python run_cloud5_seed.py
前置：SSH 隧道 127.0.0.1:18006 → 服务器 C 6006（ft serve qwen3.6-35b-a3b-fp8）。
产出：workspace/forge/blueprint.json + bible/* + outline/（卷 1 细纲 1-1..1-5）
      + forge 商讨 transcript（ask.answer 逐项留痕）。
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
from novelist.forge.io_console import AnswerIO
from novelist.forge.slots import slots_for_genre
from novelist.providers.lmstudio import LMStudioProvider
from novelist.storage.workspace import Workspace

BRIEF = (
    "都市高武与修仙结合：大学生李天劫（男，23岁），原是一名蓝星某大学计算机专业大学生，"
    "某天被大运撞死转生到了一个修仙世界，但李天劫天赋异禀，硬是修炼到了最高境界渡劫圆满，"
    "结果飞升时出现问题，回到了自己死过去了一年以后的蓝星：他发现，在自己死时，世界发生了"
    "灵气复苏，蓝星出现了可怕的诡异，但人们同时也有了借助灵气修炼的能力。由于只过去了一年，"
    "所以人们的修为普遍不高，各种功法也普遍不强，渡劫圆满的李天劫在蓝星几乎是降维打击。"
    "他决定低调种田，顺便保护家人，却在低调中不断被卷入各种事件。"
)
PID = "proj-cloud5-" + time.strftime("%Y%m%d-%H%M%S")
LOG = Path(__file__).resolve().parent / "cloud5_seed_log.txt"


def make_provider() -> LMStudioProvider:
    return LMStudioProvider(
        base_url="http://127.0.0.1:18006",
        model="qwen3.6-35b-a3b-fp8",
        reasoning_effort="none",   # FT 接入硬约束：不传则思考吃光预算
        reasoning_aware=True,
        timeout_s=600,
    )


class ScriptedConsult:
    """商讨问答的脚本化终端：is_tty=True 触发真实引擎路径（LLM 候选 + 单行解析）。

    回答策略（可复现的功能测试）：
    - 第 1 轮第 1 题回答 "1"（选编号路径，取 LLM 候选 1，src=user）
    - 其余全部回车（全取推荐值，src=候选来源）
    - 全程逐行落盘 cloud5_seed_log.txt 供人工审查
    """

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
        # 第 1 轮：对第 1 题回答 "1"（选编号）；此后全部回车取推荐
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

    bres = build(ws, PID, provider=provider, max_calls=60, resume=False, deepen=True)
    for w in bres.warnings:
        print(f"  [warn] {w}", flush=True)
    print(f"build: calls={bres.calls_used} nodes={bres.nodes_done} "
          f"卷={bres.volumes_written} 章={bres.chapters_written} ok={bres.ok}", flush=True)
    if bres.gate_halted:
        print("审核闸门挂起，待处置:", bres.pending_review, flush=True)
    print(f"\n== 完成：细纲见 novel_workspace/{PID}/outline/chapters/ ==", flush=True)


if __name__ == "__main__":
    main()
