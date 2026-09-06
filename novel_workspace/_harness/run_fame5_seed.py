"""云端 C 服务器 10 章链路 · 阶段 1：seed + 商讨 + 拍板注入 + build + 卷2细纲补齐。

需求：杨铭（大学生穿越，天玄大陆玄剑宗杂役）+ 出名就变强系统（好名恶名/真名化名
等效，声望播报）；明面真名扬善名，私下化名易容扬恶名；单女主；平推爽文
（吃瘪必报复）；系统全程不暴露、无被识破情节；天花板=渡劫九层（每境九阶）。
规模：3卷×8章；本次写 1-1..1-8 + 2-1..2-2 共 10 章（跨卷，卷1末审计）。

隧道 127.0.0.1:18006 → C:6006（FreeToken Qwen3.6-35B-A3B-FP8），由 start_c_serve.py 拉起。
用法：python run_fame5_seed.py
产出：proj-fame5-*/…（蓝图 + bible + outline/chapters/1-1..1-8, 2-1, 2-2）
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "novel_workspace" / "_harness"))

from novelist.forge import build, run_seed
from novelist.forge.ask import run_consult
from novelist.forge.nodes import NodeContext, run_node
from novelist.forge.slots import Blueprint, slots_for_genre
from novelist.providers.lmstudio import LMStudioProvider
from novelist.storage.workspace import Workspace

BRIEF = (
    "修仙爽文：大学生杨铭（男）意外车祸，穿越到天玄大陆，成为玄剑宗的一名普通杂役。"
    "穿越后觉醒了【出名就变强系统】：在某个范围内，根据自己的出名程度获得全方位的加强"
    "——修为、修炼速度、悟性等都会随名声提升；无论好名还是恶名都有效，无论用真名还是"
    "化名闯出的名头都算数；系统会以【声望+30】这类播报即时反馈。系统有更高层的来历与"
    "目的（谁在按名声发放力量），作为贯穿全书的暗线，最后才揭示。"
    "杨铭的双面经营：明面上用真名做乐善好施的大好人，扬善名；私下用化名+易容等手段"
    "坏事做尽，扬恶名——善名恶名两路并进，全都变成自己的修为。整体按爽文来：主角不吃瘪，"
    "吃瘪了一定能报复回来。最终目的：名震整个天玄大陆乃至全世界，成为世界最强者。"
    "系统本身全程保密，全书通下来不暴露，不必有任何\"被识破\"的情节。"
    "境界体系：炼气、筑基、金丹、元婴、化神、合体、炼虚、大乘、渡劫，每个境界分九个小阶，"
    "力量天花板为渡劫九层。感情线单女主。节奏为平推爽文：扬名反馈密集，每章都有"
    "出名→变强的爽点循环。开篇节奏：金手指速觉醒，第一章穿越+觉醒，第三章前完成首次"
    "出名→变强验证。线索预案——主线：名震世界之路；支线：杂役晋升线（宗门内地位）、"
    "恶名双刃线（恶名带来通缉悬赏与仇家的代价，也带来修为）、化名双身份线（善名恶名"
    "两条身份互相借力、又互相牵连）；暗线：系统真相（hidden，终局揭示）。"
)
PID = "proj-fame5-" + time.strftime("%Y%m%d-%H%M%S")
LOG = Path(__file__).resolve().parent / "fame5_seed_log.txt"

MODEL = "qwen3.6-35b-a3b-fp8"


def make_provider() -> LMStudioProvider:
    return LMStudioProvider(
        base_url="http://127.0.0.1:18006",
        model=MODEL,
        enable_thinking=False,
        reasoning_aware=True,
        timeout_s=1800,
    )


class ScriptedConsult:
    """商讨问答的脚本化终端（与 run_cloudb5_seed.py 相同策略：全取推荐值）。"""

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


def apply_verdicts(bp: Blueprint) -> list[str]:
    """用户拍板值显式注入蓝图（商讨轮默认值 ≠ 拍板值；provenance=user）。"""
    warns: list[str] = []
    ps = dict(bp.get("worldview.power_system") or {})
    ps.update({
        "levels": ["炼气", "筑基", "金丹", "元婴", "化神", "合体", "炼虚", "大乘", "渡劫"],
        "ceiling": "渡劫九层（世俗之巅；传说有人破境而去，留终局钩子）",
        "mechanic": (str(ps.get("mechanic") or "") + "；每境界分九个小阶").strip("；"),
    })
    verdicts: dict[str, object] = {
        "meta.pace": "平推爽文",
        "meta.romance": "单女主",
        "meta.opening": "金手指速觉醒",
        "worldview.name": "天玄大陆",
        "worldview.power_system": ps,
        "worldview.map": "天玄大陆：青石镇（玄剑宗杂役院）→ 玄剑宗 → 一州 → 中州 → 天下（随卷扩张）",
        # 商讨默认结局含"公开揭露系统存在"——与用户拍板"系统全程不暴露"冲突，强制覆盖
        "meta.endgame": "杨铭以善恶双名登顶天玄大陆最强；系统全程保密不暴露"
                        "（全书无被识破情节），系统真相仅对读者在终局揭示",
    }
    for k, v in verdicts.items():
        bp.set(k, v)
        bp.set_provenance(k, "user", 1.0)
    # 反派修正（用户拍板）：起点反派=杂役院执事（内部压迫者），商讨默认的州域级
    # 正道魁首不合口径——重写 rival 角色卡，州域级对手留待后卷由卷纲引入。
    for c in bp.section("characters"):
        if c.get("role") == "rival":
            c["name"] = "刘执事"
            c["core_traits"] = ["贪吝", "刻薄", "擅专营"]
            c["arc"] = "玄剑宗杂役院执事：盘剥克扣杂役、垄断扬名机会，以杂役血汗养自家修炼——主角扬名路的第一个障碍"
            bp.set_provenance("characters[role:rival].name", "user", 1.0)
            bp.set_provenance("characters[role:rival].arc", "user", 1.0)
            break
    for c in bp.section("characters"):
        if c.get("role") == "protagonist":
            c["core_traits"] = ["点子多", "演商高", "厚脸皮"]
            bp.set_provenance(f"characters[{c['id']}].core_traits", "user", 1.0)
            break
    else:
        warns.append("蓝图无 protagonist——核心性格未注入（build 后人工补）")
    return warns


def main() -> None:
    ws = Workspace(root=str(ROOT / "novel_workspace"))
    provider = make_provider()

    print(f"== 项目 {PID} ==", flush=True)
    print("== 阶段 1a：seed 提炼（smoke，不构建）==", flush=True)
    res = run_seed(ws, PID, BRIEF, provider=provider, mode="auto", smoke=True,
                   genre_pack="修仙男频", volumes=3, chapters_per_volume=8,
                   target_words=2400,
                   craft=["system-flow", "chapter-rhythm", "foreshadowing"])
    for w in res.warnings:
        print(f"  [warn] {w}", flush=True)
    print(f"seed smoke ok={res.ok} spec.template={res.spec.template_suggestion if res.spec else '-'}",
          flush=True)

    print("\n== 阶段 1b：商讨问答（脚本化，全取推荐值）==", flush=True)
    from novelist.forge import genres as _genres
    from novelist.forge.state import ForgeState

    bp = Blueprint.load(ws, PID)
    io = ScriptedConsult(LOG)
    consult = run_consult(ws, PID, bp, provider=provider, io=io,
                          slots=slots_for_genre(_genres.load_pack("修仙男频")))
    for w in consult.warnings:
        print(f"  [warn] {w}", flush=True)
    print(f"consult: answered={consult.answered} rounds={consult.rounds_done} "
          f"quit_early={consult.quit_early}", flush=True)

    state = ForgeState.load(ws, PID)
    state.touch_stage(ws, PID, "consulted")
    state.save(ws, PID)

    print("\n== 阶段 1c：拍板值注入 ==")
    bp = Blueprint.load(ws, PID)
    for w in apply_verdicts(bp):
        print(f"  [warn] {w}", flush=True)
    bp.save(ws, PID)
    print("verdicts: pace=平推爽文 romance=单女主 opening=金手指速觉醒 "
          "world=天玄大陆 ceiling=渡劫九层 ✓", flush=True)

    print("\n== 阶段 1d：全权构建（book→深化→volume→细纲，闸门自动循环）==", flush=True)
    from novelist.forge.review import REVIEW_MODULES, resolve_pending

    bres = None
    for attempt in range(20):
        bres = build(ws, PID, provider=provider, max_calls=80, resume=(attempt > 0),
                     deepen=True, coherence_review=True)
        for w in bres.warnings:
            print(f"  [warn] {w}", flush=True)
        print(f"[round {attempt+1}] calls={bres.calls_used} nodes={bres.nodes_done} "
              f"卷={bres.volumes_written} 章={bres.chapters_written} ok={bres.ok}", flush=True)
        if bres.gate_halted:
            for m in bres.pending_review:
                resolve_pending(ws, PID, m, decision="approved", remember=False)
                print(f"  auto-approve {m} ✓", flush=True)
            continue
        break
    if bres is None or (not bres.ok and bres.gate_halted):
        print("== build 未完成，终止（先排查再重跑）==", flush=True)
        return

    print("\n== 阶段 1e：卷 2 细纲补齐（2-1、2-2，nodes.run_node 同款模式）==", flush=True)
    bp = Blueprint.load(ws, PID)
    pack = None
    try:
        from novelist.forge.genres import load_pack
        pack = load_pack("修仙男频")
    except Exception:      # noqa: BLE001 - 包缺失降级
        pass
    gist_rows = {(int(r.get("vol") or 0), int(r.get("ch") or 0)): r
                 for r in bp.section("chapters") if r.get("id")}
    for vol, ch in ((2, 1), (2, 2)):
        prev = gist_rows.get((vol, ch - 1)) or gist_rows.get((1, 8))
        ctx = NodeContext(ws=ws, project_id=PID, bp=bp, provider=provider,
                          pack=pack, spec=None, vol=vol, ch=ch, prev_gist=prev,
                          arcs=None)
        r = run_node(ctx, "chapter")
        print(f"  chapter {vol}-{ch}: ok={r.ok} warns={len(r.warnings)} "
              f"tokens_out={r.tokens_out}", flush=True)
        for w in r.warnings:
            print(f"    [warn] {w}", flush=True)
        bp.save(ws, PID)

    gists = sorted(p.name for p in ws._abs(f"{PID}/outline/chapters").glob("*.md"))  # noqa: SLF001
    print(f"\n== 完成：细纲 {len(gists)} 份 {gists} ==", flush=True)
    print("下一步：人审蓝图（lines/threads 骨架）与细纲 lines_present，然后跑 run_fame5_gen.py",
          flush=True)


if __name__ == "__main__":
    main()
