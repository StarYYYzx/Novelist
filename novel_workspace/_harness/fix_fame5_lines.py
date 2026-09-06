"""proj-fame5 线索账本修复（确定性零 LLM）：

问题：build 时 book 节点只产出 2 条线（main_growth/subplot_romance），章纲模型
自造 7 个悬空 ln:id 全部被"账本无此线"忽略 → 事件层线卡注入缺线。

修复：
1. 悬空 id 语义归并映射（细纲 frontmatter lines_present + 行内「本章线索:」同步替换）
2. 账本补 4 条线（alias/infamy/shadow_king/system_truth）
3. 按映射后动作逐章重放 apply_chapter_actions（构建期本应落账的路径）
4. 1-3 手工补恶名线声明（人审修订）→ 走 replay_chapter_lines 演示 revise 通道
5. sync_bible 导出 bible/lines.json
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from novelist.core import lines as L
from novelist.core.lines import apply_chapter_actions, replay_chapter_lines
from novelist.forge.nodes import sync_bible
from novelist.forge.state import Blueprint
from novelist.storage.workspace import Workspace

PID = "proj-fame5-20260906-124016"
K = 8

# 1) 悬空 id → 规范 id（语义归并；动作与 note 保留）
REMAP = {
    "ln:system_mechanic": "ln:system_truth",
    "ln:system": "ln:system_truth",
    "ln:system_awakening": "ln:system_truth",
    "ln:system_glitch": "ln:system_truth",
    "ln:identity_risk": "ln:alias",
    "ln:lin_qingyue_suspicion": "ln:subplot_romance",
    "ln:shadow_king_investigation": "ln:shadow_king",
}
# open 归并：2-1 的"系统正式激活"与 1-1 重复（系统第 1 章已觉醒）→ advance
FORCE_ACTION = {("ln:system_truth", (2, 1)): "advance"}

NEW_LINES = [
    dict(id="ln:alias", kind="subplot",
         desc="化名双身份线：白衣剑仙（善名·真名扬）与幽冥使者（恶名·化名行）双重身份的经营、借力与牵连",
         members=["char:yangming"], target={"vol": 3, "note": "双身份终局合流，善恶双名登顶"}),
    dict(id="ln:infamy", kind="subplot",
         desc="恶名双刃线：幽冥使者恶名带来的修为滋养，与通缉悬赏、仇家追查的代价并存",
         members=["char:yangming"], target={"vol": 2, "note": "恶名反噬在卷 2 外域历练中集中爆发"}),
    dict(id="ln:shadow_king", kind="subplot",
         desc="影皇追查线：「影」字令牌势力追查幽冥使者真实身份，逐步逼近杨铭",
         members=["char:shadow_king"], target={"vol": 3, "note": "影皇成为登顶路上的磨刀石"}),
    dict(id="ln:system_truth", kind="hidden",
         desc="系统真相（暗线）：谁在按名声发放力量——系统的来历与目的，终局对读者揭示",
         target={"vol": 3, "note": "渡劫九层时揭晓，全书不暴露给书中人"}),
]


def _patch_gist(path: Path) -> tuple[list[dict], list[dict]]:
    """替换细纲中的悬空 id（frontmatter + 行内），返回 (旧声明, 新声明)。"""
    text = path.read_text(encoding="utf-8")
    head, sep, body = text.partition("---\n")
    fm = json.loads(sep and body.split("---\n")[0] or "{}") if sep else {}
    # frontmatter
    old_fm = fm.get("lines_present") or []
    new_fm = []
    for a in old_fm:
        lid = REMAP.get(str(a.get("id")), str(a.get("id")))
        act = FORCE_ACTION.get((lid, None)) or a.get("action")
        new_fm.append({"id": lid, "action": act, "note": str(a.get("note") or "")})
    fm["lines_present"] = new_fm
    # 行内「本章线索:」
    m = re.search(r"^本章线索[:：]\s*(\[.*\])\s*$", body, re.M)
    old_inline: list[dict] = []
    if m:
        try:
            old_inline = json.loads(m.group(1))
        except ValueError:
            old_inline = []
    new_inline = []
    for a in old_inline:
        lid = REMAP.get(str(a.get("id")), str(a.get("id")))
        act = FORCE_ACTION.get((lid, None)) or a.get("action")
        new_inline.append({"id": lid, "action": act, "note": str(a.get("note") or "")})
    if new_inline:
        body = re.sub(r"^本章线索[:：]\s*\[.*\]\s*$",
                      "本章线索: " + json.dumps(new_inline, ensure_ascii=False),
                      body, flags=re.M)
    fm_json = json.dumps(fm, ensure_ascii=False, indent=2)
    path.write_text("---\n" + fm_json + "\n---\n" + body, encoding="utf-8")
    return old_fm, new_inline


def main() -> None:
    ws = Workspace(root=str(ROOT / "novel_workspace"))
    bp = Blueprint.load(ws, PID)

    # ---- 1) 账本：既有 2 条 + 补 4 条 ----
    ledger = L.load_lines(ws, PID)
    print(f"[before] ledger={len(ledger)} 条: {[x['id'] for x in ledger]}")
    have = {x["id"] for x in ledger}
    for row in NEW_LINES:
        if row["id"] in have:
            continue
        kind = row.pop("kind")
        ln = L.new_line(row.pop("id"), row.pop("desc"), kind=kind, **{
            k: v for k, v in row.items() if k in ("members", "target")})
        ledger.append(ln)
    L.save_lines(ws, PID, ledger)

    # ---- 2) 细纲 id 映射替换 ----
    gist_rows = {}
    for vol in (1, 2):
        for ch in range(1, K + 1):
            p = ws.outline_chapter_path(PID, vol, ch)
            if not p.exists():
                continue
            old, new = _patch_gist(p)
            if new:
                gist_rows[(vol, ch)] = new
                print(f"[remap] {vol}-{ch}: {[a['id'] + ':' + a['action'] for a in new]}")

    # ---- 3) 1-3 人审修订：补恶名线声明 → revise 通道实机演示 ----
    p13 = ws.outline_chapter_path(PID, 1, 3)
    text = p13.read_text(encoding="utf-8")
    add = [{"id": "ln:infamy", "action": "open",
            "note": "幽冥使者恐怖传说在底层弟子中发酵——恶名开始滋养修为（人审补登）"}]
    if "ln:infamy" not in text:
        body = text.partition("---\n")[2]
        body = body.split("---\n", 1)[1]      # 跳过 frontmatter 尾
        body = body.rstrip() + "\n\n本章线索: " + json.dumps(add, ensure_ascii=False) + "\n"
        head_fm = json.loads(text.split("---\n")[1])
        head_fm["lines_present"] = add
        p13.write_text("---\n" + json.dumps(head_fm, ensure_ascii=False, indent=2)
                       + "\n---\n" + body, encoding="utf-8")
    warns = replay_chapter_lines(ws, PID, 1, 3, K)
    print(f"[replay 1-3] {warns}")

    # ---- 4) 其余章按映射后动作落账（构建期悬空未落，直接重放）----
    ledger = L.load_lines(ws, PID)
    for (vol, ch), acts in sorted(gist_rows.items()):
        if (vol, ch) == (1, 3):
            continue                          # 已走 replay
        w = apply_chapter_actions(ws, PID, ledger, vol, ch, acts)
        for x in w:
            print(f"[apply {vol}-{ch}] warn: {x}")
    L.save_lines(ws, PID, ledger)

    # ---- 5) 蓝图 lines 段同步 + 导出 bible/lines.json ----
    bp = Blueprint.load(ws, PID)
    bp.data["lines"] = L.load_lines(ws, PID)
    bp.save(ws, PID)
    written = sync_bible(ws, PID, bp)
    final = L.load_lines(ws, PID)
    print(f"[after] ledger={len(final)} 条")
    for ln in final:
        print(f"  {ln['id']} {ln['kind']} {ln['status']} opened={ln.get('opened')} "
              f"progress={len(ln.get('progress') or [])}条")
    print("sync_bible:", [w for w in written if "lines" in w])


if __name__ == "__main__":
    main()
