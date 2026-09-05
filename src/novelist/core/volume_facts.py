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


def build_volume_facts(ws, project_id: str, provider, vol: int,
                       max_tokens: int = 2000) -> bool:
    """卷全部章生成完后调用：通读本卷正文 → 事实清单落盘。成功 True。"""
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
    # 超长卷截尾保护（预算两头夹击教训）：保开头 + 保末章结尾
    if len(body) > 24000:
        body = body[:16000] + "\n\n……（中略）……\n\n" + body[-6000:]
    prompt = _PROMPT.format(vol=vol) + body
    try:
        res = provider.complete(LLMRequest(
            messages=[LLMMessage(role="user", content=prompt)],
            max_tokens_out=max_tokens, temperature=0.3))
        raw = (res.content or "").strip() if getattr(res, "ok", False) else ""
    except Exception:  # noqa: BLE001
        return False
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


def facts_path(ws, project_id: str, vol: int):
    return ws._abs(project_id + "/" + FACTS_REL.format(vol=vol))  # noqa: SLF001


def load_prev_facts(ws, project_id: str, vol: int, max_chars: int = 1200) -> str:
    """生成侧读取：第 vol-1 卷的事实清单（下一卷每章注入 goal）。"""
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
    if len(t) > max_chars:
        t = t[:max_chars] + "\n……（截断）"
    return t


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
