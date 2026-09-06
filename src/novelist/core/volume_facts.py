"""卷末事实清单（批次三·方案3）。

## 动机

worldstate 确定性合成抓不住**语义**（已知债：worldstate.time 静止、伏笔承诺无人记账）。
一卷写完后，让 LLM 通读本卷全部正文，产出"截至本卷的事实清单"：

1. **人物状态**——境界/伤势/阵营/关系变化（谁跟谁翻脸了、谁欠了谁人情）；
2. **时间线**——故事内时间推进到的点（补 worldstate.time 静止的债）；
3. **伏笔承诺**——本卷抛出但尚未回收的钩子（契诃夫之枪的"枪架上了"清单）；
4. **未决冲突**——下一卷必须接住的悬念。

产物：``workspace/forge/volume-facts-v{vol}.md``（人读+机读两用）。
下一卷每章生成时，由 orchestrator 注入前卷事实清单（goal 钉死层），
压制跨卷的状态漂移与伏笔遗失。

与纯确定性 worldstate 合成的关系：**互补而非替代**——worldstate 照旧走
确定性增量，事实清单补语义层。LLM 幻觉风险由"只记正文中明确发生的事"约束。
"""
from __future__ import annotations

import re

from .llm import LLMMessage, LLMRequest

FACTS_REL = "workspace/forge/volume-facts-v{vol}.md"

_PROMPT = (
    "你是长篇小说的连载编辑。以下是第 {vol} 卷全部正文章节。请通读后输出"
    "「截至第 {vol} 卷末的事实清单」，Markdown 格式，只记**正文中明确发生的事**，"
    "严禁推测或补写。结构固定为四节：\n\n"
    "## 人物状态\n每条一行：`- 名字：状态变化`（境界/伤势/阵营/关系/持有物，"
    "只写本卷内发生变化的）\n\n"
    "## 时间线\n`- 故事内时间推进：从 X 到 Y`（依据正文中的时间标记；无法判断写「未明」）\n\n"
    "## 未回收伏笔\n`- [第N章] 伏笔描述`（本卷抛出但尚未兑现的钩子/承诺/悬念）\n\n"
    "## 未决冲突\n`- 冲突描述`（下一卷必须接住的对抗/债务/约定）\n\n"
    "正文：\n\n")

# 两段式第一段：逐章摘要（每章一次小请求，输入 ~2-3k 字，远低于显存硬阈值）
_CHUNK_PROMPT = (
    "你是连载编辑。以下是第 {vol} 卷第 {ch} 章正文。只记**正文中明确发生的事**，"
    "严禁推测。输出 3-6 行要点：人物状态变化（境界/伤势/关系/持有物）、"
    "明确的时间标记、本章抛出且未在本章内兑现的钩子、遗留的对抗/约定。"
    "每行一条，不要小节标题，不要评论。\n\n正文：\n\n")

# 两段式第二段：摘要合并为四节清单（输入只有各章摘要，~1-2k 字）
_MERGE_PROMPT = (
    "你是长篇小说的连载编辑。以下是第 {vol} 卷各章的要点摘录（按章序）。请合并输出"
    "「截至第 {vol} 卷末的事实清单」，Markdown 格式，只记**摘录中明确提到的事**。"
    "结构固定为四节：\n\n"
    "## 人物状态\n每条一行：`- 名字：状态变化`\n\n"
    "## 时间线\n`- 故事内时间推进：从 X 到 Y`（无法判断写「未明」）\n\n"
    "## 未回收伏笔\n`- [第N章] 伏笔描述`\n\n"
    "## 未决冲突\n`- 冲突描述`\n\n"
    "各章摘录：\n\n")

# 单请求输入安全阈值（字符）：12GB 显存 + MoE offload 实测——权重已占 11.5GB，
# 长输入 prefill 激活直接 CUDA OOM 且后端 worker 不可自愈（2026-09-05 真机两连崩）。
# 9.6k 字输入必死；章节生成的 ~4-5k 字请求全程存活。取 6000 为保守分块线。
_MAX_REQUEST_CHARS = 6000


def _complete_small(provider, prompt: str, max_tokens: int) -> str:
    """小请求补全：失败返回空串（增强层语义）。"""
    try:
        res = provider.complete(LLMRequest(
            messages=[LLMMessage(role="user", content=prompt)],
            max_tokens_out=max_tokens, temperature=0.3,
            thinking=True))  # 判断类：卷事实抽取，开思考
        return (res.content or "").strip() if getattr(res, "ok", False) else ""
    except Exception:  # noqa: BLE001
        return ""


def build_volume_facts(ws, project_id: str, provider, vol: int,
                       max_tokens: int = 2000) -> bool:
    """卷全部章生成完后调用：通读本卷正文 → 事实清单落盘。成功 True。

    两段式分块（2026-09-05）：单请求整卷正文在 12GB 显存服务器上会 CUDA OOM
    （权重占满 + 长 prefill 激活超限，且 OOM 后后端不可自愈）。正文合计超过
    `_MAX_REQUEST_CHARS` 时改走"逐章小请求摘要 → 合并"两段式，每段输入均
    远低于阈值；小卷仍走原单请求路径。
    """
    if provider is None:
        return False
    # 收集本卷已写章节（存在且非空的草稿）
    parts: list[str] = []
    ch = 1
    while True:
        p = ws.draft_path(project_id, vol, ch)
        if not p.exists():
            break
        try:
            t = p.read_text(encoding="utf-8").strip()
        except OSError:
            break
        if t:
            parts.append(f"### 第 {vol}-{ch} 章\n{t}")
        ch += 1
    if len(parts) < 1:
        return False
    body = "\n\n".join(parts)
    total = sum(len(p) for p in parts)
    if total > _MAX_REQUEST_CHARS:
        return _build_volume_facts_chunked(ws, project_id, provider, vol, parts,
                                           max_tokens)
    # 超长卷截尾保护（预算两头夹击教训）：保开头 + 保末章结尾
    if len(body) > 24000:
        body = body[:16000] + "\n\n……（中略）……\n\n" + body[-6000:]
    raw = _complete_small(provider, _PROMPT.format(vol=vol) + body, max_tokens)
    if not raw or "##" not in raw:
        return False
    try:
        header = (f"<!-- 第 {vol} 卷事实清单（LLM 生成，只记正文明确发生的事） -->\n"
                  f"# 第 {vol} 卷末事实清单\n\n")
        ws.write_text(ws._abs(project_id + "/" + FACTS_REL.format(vol=vol)),  # noqa: SLF001
                      header + raw + "\n")
        return True
    except Exception:  # noqa: BLE001
        return False


def _build_volume_facts_chunked(ws, project_id: str, provider, vol: int,
                                parts: list[str], max_tokens: int) -> bool:
    """两段式：逐章摘要（小请求）→ 合并为四节清单（小请求）。任一章摘要失败即放弃。"""
    notes: list[str] = []
    for part in parts:
        head = part.split("\n", 1)[0]          # "### 第 v-c 章"
        text = part.split("\n", 1)[1] if "\n" in part else ""
        m = re.match(r"### 第 (\d+)-(\d+) 章", head)
        cv, cc = (m.group(1), m.group(2)) if m else (str(vol), "?")
        # 单章正文仍超限的极端情况：保开头+结尾截尾（章节很少超，兜底用）
        if len(text) > _MAX_REQUEST_CHARS - 800:
            keep = _MAX_REQUEST_CHARS - 800
            text = text[:keep // 2] + "\n……（中略）……\n" + text[-keep // 2:]
        note = _complete_small(provider, _CHUNK_PROMPT.format(vol=cv, ch=cc) + text,
                               800)
        if not note:
            return False
        notes.append(f"### 第 {cv}-{cc} 章\n{note}")
    raw = _complete_small(provider,
                          _MERGE_PROMPT.format(vol=vol) + "\n\n".join(notes),
                          max_tokens)
    if not raw or "##" not in raw:
        return False
    try:
        header = (f"<!-- 第 {vol} 卷事实清单（两段式分块生成，只记正文明确发生的事） -->\n"
                  f"# 第 {vol} 卷末事实清单\n\n")
        ws.write_text(ws._abs(project_id + "/" + FACTS_REL.format(vol=vol)),  # noqa: SLF001
                      header + raw + "\n")
        return True
    except Exception:  # noqa: BLE001
        return False


def facts_path(ws, project_id: str, vol: int):
    return ws._abs(project_id + "/" + FACTS_REL.format(vol=vol))  # noqa: SLF001


def load_prev_facts(ws, project_id: str, vol: int, max_chars: int = 1200) -> str:
    """生成侧读取：第 vol-1 卷的事实清单（下一卷每章注入 goal）。

    H7 修复（2026-09-05）：原 head-only 截断会把排在清单末尾的"未回收伏笔 /
    未决冲突"两节整节切掉——恰是跨卷最要命的信息。改为**分节配额**：
    四节均分预算，各节内部超长再截，保证每节都进 prompt。
    """
    if vol <= 1:
        return ""
    try:
        p = facts_path(ws, project_id, vol - 1)
        if not p.exists():
            return ""
        t = p.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return ""
    if not t:
        return ""
    if len(t) <= max_chars:
        return t
    # 切四节（## 人物状态 / ## 时间线 / ## 未回收伏笔 / ## 未决冲突）
    heads = [m.start() for m in re.finditer(r"^## ", t, re.M)]
    prelude = t[:heads[0]].strip() if heads else ""
    sections: list[str] = []
    for i, h in enumerate(heads):
        end = heads[i + 1] if i + 1 < len(heads) else len(t)
        sections.append(t[h:end].strip())
    per = max(max_chars // max(len(sections), 1), 200)
    out: list[str] = []
    used = len(prelude)
    if prelude and used < max_chars // 3:
        out.append(prelude)
    for sec in sections:
        if used >= max_chars:
            break
        if len(sec) > per:
            sec = sec[:per] + "\n……（本节截断）"
        out.append(sec)
        used += len(sec) + 2
    result = "\n\n".join(out)
    return result[:max_chars] if len(result) > max_chars else result


def compact_for_polish(ws, project_id: str, vol: int, max_chars: int = 500) -> str:
    """润色用的极简摘要：只取人物状态 + 未回收伏笔两节的前几行。"""
    t = load_prev_facts(ws, project_id, vol + 1, max_chars=10**9)
    if not t:
        return ""
    keep: list[str] = []
    for section in ("## 人物状态", "## 未回收伏笔"):
        idx = t.find(section)
        if idx < 0:
            continue
        nxt = len(t)
        for other in ("## ",):
            j = t.find(other, idx + len(section))
            if j > idx:
                nxt = min(nxt, j)
        keep.append(t[idx:nxt].strip())
    out = "\n\n".join(keep)
    return out[:max_chars]
