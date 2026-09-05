"""云端 B 服务器（T4 llama.cpp Qwen3.8-27B）五章链路 · 阶段 1：seed + 商讨 + 构建。

与 run_cloud5_seed.py 的差异仅在后端：
- 隧道 127.0.0.1:16012 → B:6012（llama-server Qwen3.8-27B-UD-IQ3_S，ctx 8192 --jinja）
- 关思考走 chat_template_kwargs.enable_thinking=false（provider enable_thinking=False）
- T4 decode ~7.5 tok/s：timeout_s 放大到 1800

用法：python run_cloudb5_seed.py
产出：proj-cloudb5-*/workspace/forge/blueprint.json + bible/* + outline/（细纲 1-1..1-5）
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
    "都市高武与修仙结合：大学生李天劫（男，23岁），原是一名蓝星某大学计算机专业大学生，"
    "某天被大运撞死转生到了一个修仙世界，但李天劫天赋异禀，硬是修炼到了最高境界渡劫圆满，"
    "结果飞升时出现问题，回到了自己死过去了一年以后的蓝星：他发现，在自己死时，世界发生了"
    "灵气复苏，蓝星出现了可怕的诡异，但人们同时也有了借助灵气修炼的能力。由于只过去了一年，"
    "所以人们的修为普遍不高，各种功法也普遍不强，渡劫圆满的李天劫在蓝星几乎是降维打击。"
    "他决定低调种田，顺便保护家人，却在低调中不断被卷入各种事件。"
)
PID = "proj-cloudb5-" + time.strftime("%Y%m%d-%H%M%S")
LOG = Path(__file__).resolve().parent / "cloudb5_seed_log.txt"

MODEL = "/root/autodl-tmp/models/Qwen3.8-27B-UD-IQ3_S.gguf"


def make_provider() -> LMStudioProvider:
    return LMStudioProvider(
        base_url="http://127.0.0.1:16012",
        model=MODEL,
        enable_thinking=False,   # llama.cpp v0.4.0：chat_template_kwargs 才有效
        reasoning_aware=True,
        timeout_s=1800,
    )


class ScriptedConsult:
    """商讨问答的脚本化终端（与 run_cloud5_seed.py 相同策略）。"""

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
