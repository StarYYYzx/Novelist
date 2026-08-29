"""系统整体测试驱动：《断玉青冥》串行写章 + 记忆编纂。

⚠️ 本脚本**代做了系统尚未实现的两件事**，用以后续判定缺口的严重性：

1. **圣经注入生成上下文**——`produce_chapter` 默认 system_prompt 是"你是主编剧，负责指挥创作。"，
   世界观/文风/人物卡从未进入 LLM 上下文（实测导致主角性别漂移、卷名漂移、凭空造人）。
   本脚本自行拼装 system_prompt 传入，属于补偿行为。
2. **记忆编纂（事件抽取）**——docs/05 §3 定义的「记忆编纂员」子代理不存在
   （`core/subagent.py` 未实现）。本脚本代行其职：让 LLM 从成章正文抽取事件，
   再经系统自己的 `MemoryWriter`（含冲突双检）写入 memory/。

除此之外全部走系统自身代码路径：Workspace / produce_chapter / MemoryRetriever /
MemoryWriter / commit_event。

用法：
    python novel_workspace/_harness/run_novel.py [--from-ch N] [--chapters N] [--tokens N]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from novelist.core.embedding import make_embedding  # noqa: E402
from novelist.core.llm import LLMMessage, LLMRequest  # noqa: E402
from novelist.core.memory import (  # noqa: E402
    MemoryConflictError,
    MemoryIndex,
    MemoryQuery,
    MemoryRetriever,
    MemoryWriter,
)
from novelist.core.orchestrator import produce_chapter  # noqa: E402
from novelist.core.session import SessionInfo  # noqa: E402
from novelist.providers.lmstudio import LMStudioProvider  # noqa: E402
from novelist.storage.workspace import Workspace  # noqa: E402

PID = "proj-duanyu"
WS_ROOT = ROOT / "novel_workspace"
MODEL = "qwen/qwen3.5-9b"

ws = Workspace(root=str(WS_ROOT))


def _read(rel: str, default=None):
    p = ws._abs(f"{PID}/{rel}")
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


BIBLE = {
    "characters": _read("bible/characters.json", []),
    "worldview": _read("bible/worldview.json", {}),
    "style": _read("bible/style.json", {}),
    "locations": _read("bible/locations.json", []),
    "plot_threads": _read("bible/plot_threads.json", []),
}
NAME_TO_ID = {}
for c in BIBLE["characters"]:
    NAME_TO_ID[c["name"]] = c["id"]
    for a in c.get("aliases") or []:
        NAME_TO_ID[a] = c["id"]


def chapter_cast(vol: int, ch: int) -> list[dict]:
    """本章出场人物：按 first_appear 已出现 + 细纲里点名的人物。"""
    gist = ws.outline_chapter_path(PID, vol, ch)
    text = gist.read_text(encoding="utf-8") if gist.exists() else ""
    cast = []
    for c in BIBLE["characters"]:
        fa = c.get("first_appear") or {}
        appeared = fa.get("vol", 99) < vol or (fa.get("vol") == vol and fa.get("ch", 99) <= ch)
        named = c["name"] in text
        if appeared and (named or c["id"] == "char:suwan"):
            cast.append(c)
    return cast


def build_system_prompt(vol: int, ch: int) -> str:
    """拼装 system prompt：世界观 + 文风 + 本章人物卡 + 硬约束。

    系统本身不提供这个（produce_chapter 的默认 system_prompt 只有一句话）。
    """
    st = BIBLE["style"]
    wv = BIBLE["worldview"]
    levels = (wv.get("power_system") or {}).get("levels") or []
    rules = wv.get("rules") or []

    lines = [
        "你是男频修仙小说的主编剧，正在写第 %d 卷第 %d 章。" % (vol, ch),
        "",
        "【世界设定】",
        f"世界：{wv.get('name', '')}；境界体系：{'、'.join(levels)}",
    ]
    for r in rules:
        lines.append(f"- 铁律：{r}")
    lines += [
        "",
        "【文风约束】",
        f"- 视角：{st.get('pov', '')}；时态：{st.get('tone', '') and st.get('tense', '')}",
        f"- 笔调：{'、'.join(st.get('tone') or [])}",
        f"- 叙述：{st.get('narration', '')}",
        f"- 禁用词（出现即失败）：{'、'.join(st.get('forbidden_words') or [])}",
        f"- 目标字数：约 {st.get('target_words_per_chapter', 800)} 字，必须在本章内写完一个完整段落，不要中途截断",
        "",
        "【本章出场人物】（严格按卡片写，不得改变性别/境界/阵营，不得引入新人物）",
    ]
    for c in chapter_cast(vol, ch):
        gender = {"male": "男", "female": "女"}.get(c.get("gender"), "未知")
        pw = c.get("power") or {}
        lines.append(
            f"- {c['name']}（{gender}，{pw.get('level', '')}，{pw.get('faction', '')}）："
            f"性格{'、'.join(c.get('core_traits') or [])}；人物弧线：{c.get('arc', '')}"
        )
    proto = st.get("protagonist") or {}
    if proto:
        lines.append(f"\n【特别强调】主角是{proto.get('name')}，性别为男子，所有代词一律用「他」，绝不可用「她」。")
    lines += [
        "",
        "【输出格式】",
        "直接输出正文。第一行写章节标题，格式固定为：## 第X章 标题（标题用本章细纲给定的）",
        "不要写卷名，不要写前言、后记、注释，不要输出大纲或说明。",
        "",
        "【硬性禁止】",
        "- 正文内不得出现「第一章」「本章」「上一章」「细纲」这类元叙事表述——读者不该看到章节编号。",
        "- 必须在结尾写一个完整的收束句，以句号/问号/感叹号结束，严禁写到一半停下。",
        "  宁可把内容压缩，也要保证本章结构完整。",
    ]
    return "\n".join(lines)


def recall(vol: int, ch: int, gist_text: str, embedding, top_k: int = 6) -> list[str]:
    """先忆：检索与此章细纲相关的历史记忆片段（系统能力，非补偿）。"""
    idx = MemoryIndex.load(ws, PID)
    if not idx.fragments:
        idx.rebuild(ws, PID, embedding)
    if not idx.fragments:
        return []
    hits = MemoryRetriever(idx, embedding=embedding).query(
        MemoryQuery(query=gist_text or f"第 {vol} 卷第 {ch} 章", top_k=top_k)
    )
    return [f"- [{h.kind} @ {h.source.get('vol')}:{h.source.get('ch')}] {h.text}" for h in hits if h.score > 0]


EXTRACT_PROMPT = """你是记忆编纂员。阅读下面这一章正文，提取其中**确实发生了**的剧情事件。

只输出事件行，每行一个事件，格式严格为（用竖线分隔三或四段）：
事件简述 | 类型 | 涉及人物姓名（逗号分隔）

- 类型只能是这些之一：conflict, discovery, reveal, turning_point, dialogue, departure
- 涉及人物只写正文里真实出现过的姓名，不要写不存在的名字
- 不要输出标题、序号、解释或空行
- 最多 3 条，宁少勿多

正文：
"""


def extract_events(provider, chapter_text: str) -> list[dict]:
    """代行「记忆编纂员」：LLM 从正文抽取事件。返回 [{summary, kind, names}]。"""
    res = provider.complete(
        LLMRequest(
            messages=[LLMMessage(role="user", content=EXTRACT_PROMPT + chapter_text[-2500:])],
            max_tokens_out=300,
            temperature=0.3,
        )
    )
    if res.blocked or not res.content:
        return []
    events = []
    for line in res.content.splitlines():
        line = line.strip().lstrip("-•*0123456789.、) ")
        if "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2 or not parts[0] or len(parts[0]) < 4:
            continue
        summary = re.sub(r"^事件[:：]\s*", "", parts[0])
        kind = parts[1] if parts[1] in {
            "conflict", "discovery", "reveal", "turning_point", "dialogue", "departure",
        } else "turning_point"
        names = [n.strip() for n in (parts[2].split(",") if len(parts) > 2 else []) if n.strip()]
        # 回退：模型常漏给人物名，直接从简述里回扫圣经已建档人物的姓名/别称
        for nm in NAME_TO_ID:
            if nm and nm in summary and nm not in names:
                names.append(nm)
        events.append({"summary": summary[:120], "kind": kind, "names": names})
    return events[:3]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-ch", type=int, default=1)
    ap.add_argument("--chapters", type=int, default=10)
    ap.add_argument("--tokens", type=int, default=800, help="每章生成预算（CLI 硬编码 400，不够写完一章）")
    ap.add_argument("--no-extract", action="store_true", help="跳过编纂抽取（只用系统自带的章级合成事件）")
    args = ap.parse_args()

    provider = LMStudioProvider(model=MODEL, timeout_s=600)
    embedding = make_embedding("keyword-fallback")

    log_path = WS_ROOT / "_harness" / "run_log.jsonl"
    total_events = 0
    t_all = time.time()

    for ch in range(args.from_ch, args.from_ch + args.chapters):
        vol = 1
        t0 = time.time()
        draft = ws.draft_path(PID, vol, ch)
        if draft.exists():
            print(f"[ch{ch}] draft exists, skip generation")
        else:
            gist = ws.outline_chapter_path(PID, vol, ch)
            gist_text = gist.read_text(encoding="utf-8") if gist.exists() else ""
            sys_prompt = build_system_prompt(vol, ch)
            memories = recall(vol, ch, gist_text, embedding)
            goal_parts = [
                f"请撰写第 {vol} 卷第 {ch} 章正文。",
                "细纲：",
                gist_text[:1200],
            ]
            if memories:
                goal_parts += ["", "前情提要（必须先忆，与前情保持一致）：", *memories]
            goal_parts += ["", "要求：严格按细纲推进，写完本章全部要点，结尾收在一个完整的句子上。"]

            res = produce_chapter(
                ws, PID, vol, ch, provider,
                session=SessionInfo(project_id=PID, agent="orchestrator"),
                system_prompt=sys_prompt,
                final_goal="\n".join(goal_parts),
                prefer_direct=True,
                generation_tokens=args.tokens,
                embedding=embedding,
            )
            if not res.ok:
                print(f"[ch{ch}] FAILED: {res.result[:200]}")
                continue
            print(f"[ch{ch}] generated mode={res.mode} {time.time() - t0:.0f}s -> {res.chapter_path}")

        # ---- 编纂：抽取真实事件并写入记忆层（代行编纂员）----
        if args.no_extract or not draft.exists():
            continue
        text = draft.read_text(encoding="utf-8")
        try:
            events = extract_events(provider, text)
        except Exception as e:  # noqa: BLE001
            print(f"[ch{ch}] extract error: {e}")
            events = []

        writer = MemoryWriter(ws, PID, embedding=embedding)
        written, conflicts = 0, []
        for i, ev in enumerate(events, 1):
            ids = [NAME_TO_ID[n] for n in ev["names"] if n in NAME_TO_ID]
            try:
                writer.append_plot_event({
                    "id": f"ev:{PID}:{vol}:{ch}:x{i}",
                    "at": {"vol": vol, "ch": ch},
                    "type": ev["kind"],
                    "summary": ev["summary"],
                    "participants": ids,
                    "affected_threads": [],
                })
                for cid in ids:
                    try:
                        writer.append_experience(cid, {
                            "at": {"vol": vol, "ch": ch},
                            "summary": ev["summary"],
                            "state_delta": None,
                        })
                    except MemoryConflictError:
                        pass  # 同事件同人已入库，跳过
                written += 1
            except MemoryConflictError as e:
                conflicts.append(f"{ev['summary'][:30]} :: {e}")
        total_events += written
        rec = {
            "ch": ch, "secs": round(time.time() - t0), "words": len(text),
            "extracted": len(events), "written": written, "conflicts": conflicts,
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"[ch{ch}] 编纂：抽取 {len(events)} 条，入库 {written} 条，冲突 {len(conflicts)} 条"
              + (f" | 冲突示例：{conflicts[0][:80]}" if conflicts else ""))

    print(f"\nTOTAL: {total_events} real events committed, {time.time() - t_all:.0f}s")


if __name__ == "__main__":
    main()
