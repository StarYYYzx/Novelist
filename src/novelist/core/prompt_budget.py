"""Prompt 预算器（M3t·滑动窗口上下文，用户 2026-09-04 提议）。

## 为什么需要

事件 prompt 由 _event_goal 按五层拼接（骨架/人物/上下文/知识/收尾），但**各层只有
自己的局部上限（接缝 300 字、appeared_notes 尾 12 条、RAG top4），总量无人在管**。
实害已发生过两次：

1. ch7 OOM：KV 8192 + 长上下文把 3080Ti 12GB 显存打挂，ft backend 直接崩；
2. 9B 思考 token 两头夹击：system prompt 上千字后，max_tokens ≤1400 会被思考
   吃光（content 空 + finish=length），预算越紧质量越差。

## 设计边界（与用户对齐的拍板记录）

- **预算器不是注入器**：各注入点保持不动，本模块只在拼装完成后做一次总量裁剪。
- **确定性分层淘汰**，不猜重要性：钉死层（骨架/人物卡/调度单/接缝/收尾纪律）永不淘汰；
  可淘汰层按 EVICT_PRIORITY 从低到高整段丢弃（教训 → 势力 → 回读 → 伏笔 → RAG…）。
- **淘汰必须可审计**：返回被淘汰的 tag 列表，orchestrator 记入 ProductionResult，
  绝不允许静默丢上下文（静默丢失会制造"计划态 vs 实然"那类一致性事故）。
- **单位用字符**：中文 1 字 ≈ 1 token 量级，估算足够；不引入 tokenizer 依赖。
- 默认关闭（budget=0），由 config/调用方显式开启——上线前不改变任何现有产物。

## 与"热集视图"的关系（用户机制 2）

两者是一对：热集视图（人物状态统一加载，待做）提供候选，本模块决定谁进 prompt。
本模块不读盘、不维护状态，天然满足 ADR-016"缓存可再生"约束。
"""

from __future__ import annotations

import re

# 淘汰优先级：数值越小越先淘汰。依据——离"本场写什么"越远的信息越先让路。
EVICT_PRIORITY: dict[str, int] = {
    # 历史教训/势力是长程信号，单事件里最不可能立即用到 → 最先让路
    "related:lesson": 10,
    "related:faction": 11,
    # 回读类：前章正文/久未出场者原文，块最大、与"本场"距离最远
    "extra_readback": 12,
    "readback": 13,
    "related:thread": 14,
    "memories": 15,
    "related:setting": 16,
    "hist_lines": 17,
    "settings": 18,
    "appeared": 19,   # D12 防重复登场，最后才让
    "prose_window": 20,  # 正文滑动窗口（反重演核心），可淘汰层里最后让路
    # 钉死层：永不淘汰
    "goal": -1,
    "step": -1,
    "discipline": -1,
    "direction": -1,
    "cast": -1,
    "seam": -1,
    "tail": -1,
    "first_seen": -1,  # 方案6.1：首次出场硬约束（软提示失效实证后升级钉死）
    "banned": -1,      # 方案6.4：广播拒绝点名禁令（一致性行，宁超不丢）
    "lines": -1,       # ADR-025：线索卡（事件层唯一执行层）——RAG 未命中不再等于线索沉默
    "live_state": -1,  # ADR-013 完全体：worldstate 实然状态行（事件级回写的读取侧）
}

PINNED = -1
_UNKNOWN_KEEP = 50  # 未登记 tag 一律不淘汰（宁超不误删）


def apply_prompt_budget(sections: list[tuple[str, str]], char_budget: int,
                        ) -> tuple[list[tuple[str, str]], list[str]]:
    """对拼装好的 prompt 分段做总量裁剪。

    `sections`：`[(tag, text)]`，text 为该段完整文本（含空行分隔）。
    返回 `(裁剪后 sections, 被淘汰的 tag 列表)`。总长未超预算时原样返回。

    淘汰按 EVICT_PRIORITY 升序整段进行；钉死层与未登记 tag 不动。
    """
    total = sum(len(text) for _, text in sections)
    if char_budget <= 0 or total <= char_budget:
        return sections, []
    order = sorted(
        (EVICT_PRIORITY.get(tag, _UNKNOWN_KEEP), i)
        for i, (tag, _) in enumerate(sections))
    kept = set(range(len(sections)))
    evicted: list[str] = []
    for prio, i in order:
        if prio == PINNED or prio >= _UNKNOWN_KEEP:
            continue
        kept.discard(i)
        total -= len(sections[i][1])
        evicted.append(sections[i][0])
        if total <= char_budget:
            break
    return [s for k, s in enumerate(sections) if k in kept], evicted


def prose_tail(text: str, window_chars: int) -> str:
    """正文滑动窗口切片：取 `text` 末尾约 `window_chars` 字，段落边界对齐。

    用于把已写正文的最近片段注入后续事件 prompt（用户 2026-09-04 提议），
    让模型看见"前面发生了什么"，压制同一事件重演/同一出场套路复用。
    短于窗口的原样返回；切点向前回退到最近的段落边界（空行/换行），
    保证注入的首段是完整段落而非半句。
    """
    if window_chars <= 0 or len(text) <= window_chars:
        return text
    tail = text[-window_chars:]
    m = None
    for m in re.finditer(r"\n+", tail):
        pass
    if m is not None and m.start() > 0:
        tail = tail[m.end():]
    return tail.lstrip()


def head_tail_window(text: str, max_chars: int, *, head_chars: int = 800) -> str:
    """头+尾窗口切片：超预算时保留前 `head_chars` 字 + 尾部余额，中段折叠标注。

    与同模块 `prose_tail` 的分工：`prose_tail` 服务于**接续写作**（只需要最近的下文
    接缝，切点对齐段落边界）；本函数服务于**通读判断**（审校/抽取），头尾都要——
    章头的事件、状态与**时间行**密度最高（时间行是 `worldstate.time` 推进的唯一来源），
    tail-only 会让它们静默消失（`chronicler` 的 H2 修复即此坑）。

    调用方一律走本函数，不要各写一份 `text[-N:]`——两处各写一份正是本坑的成因。
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head = min(head_chars, max(0, max_chars // 3))
    tail = max(max_chars - head, 1)
    return text[:head] + "\n……（中段略）……\n" + text[-tail:]

