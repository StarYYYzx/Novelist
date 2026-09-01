"""审校师：LLM 语义审校（docs/05 §3 / §5.4，ADR-009 语义层，B-08）。

## 它补的是什么

`consistency/rules.py` 只能判**能确定性判定**的问题（引用完整性、时间线顺序、
用词纪律、境界表述一致性）。以下这些它判不了：

- 人物称谓与身份是否匹配（实测：筑基修士称炼气三层的少年为「前辈」）
- 情节逻辑与因果是否自洽
- 人设是否漂移（性格、说话方式与人物卡不符）
- 前情事实是否被违反（已死的人复活、已毁的物品再现）
- 细纲要点是否被漏写（docs/05 的「检查员」职责）
- 伏笔是否按预期推进

审校师用 LLM 泛读章节对照圣经来发现这些。它是**只读**的：产出审计工单，
不直接改稿——修订由编排层决定（docs/04 §5.4 双层门禁的语义层）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from ..core.context import load_bible
from ..core.llm import LLMMessage, LLMRequest
from ..storage.workspace import Workspace

CATEGORIES = (
    "设定矛盾", "人设漂移", "称谓失当", "时间线", "战力越级",
    "事实前后矛盾", "细纲未覆盖", "伏笔", "其他",
)

REVIEW_PROMPT = """你是审校师。对照设定圣经与前情，审读下面这一章正文，找出问题。

【设定圣经摘要】
{bible}

【前情提要】（已发生的事实，本章不得与之矛盾）
{memory}

【本章细纲】（要点应当被落实）
{gist}

【审校维度】
- 设定矛盾：是否违反世界规则
- 人设漂移：言行是否偏离人物卡（特别注意性别、性格、说话方式）
- 称谓失当：人物之间的称呼是否符合身份与修为高低（低修为者称高修为者为「前辈」才合理）
- 时间线与因果：事件顺序、因果链是否自洽
- 战力越级：修为描写是否超出人物卡或自相矛盾
- 事实前后矛盾：与前情提要是冲突（如已死之人复活、已毁之物再现）
- 细纲未覆盖：细纲要点是否有遗漏

【输出】只输出问题行，每行：级别 | 维度 | 问题描述 | 修订建议
- 级别：block（必须改）/ warn（建议改）
- 维度只能是上述七项之一
- 没有问题就只输出一行：ok

正文：
"""


@dataclass
class ReviewIssue:
    level: str            # block | warn
    category: str
    detail: str
    suggestion: str = ""


def _bible_brief(ws: Workspace, project_id: str, cast_ids: list[str] | None = None) -> str:
    bible = load_bible(ws, project_id)
    lines: list[str] = []
    wv = bible.get("worldview") or {}
    if wv:
        levels = ((wv.get("power_system") or {}).get("levels")) or []
        if levels:
            lines.append(f"境界体系：{'、'.join(levels)}")
        for r in wv.get("rules") or []:
            lines.append(f"铁律：{r}")
    st = bible.get("style") or {}
    proto = st.get("protagonist") or {}
    if proto.get("name"):
        lines.append(f"主角：{proto['name']}（性别 {proto.get('gender', 'unknown')}）")
    chars = [c for c in bible.get("characters") or [] if isinstance(c, dict)]
    if cast_ids:
        chars = [c for c in chars if c.get("id") in cast_ids] or chars
    for c in chars[:24]:
        pw = c.get("power") or {}
        lines.append(f"- {c.get('name')}（性别 {c.get('gender', 'unknown')}"
                     + (f"，{pw.get('level')}" if pw.get("level") else "")
                     + (f"，{pw.get('faction')}" if pw.get("faction") else "")
                     + f"）：性格{'、'.join(c.get('core_traits') or []) or '—'}")
    return "\n".join(lines)


class Reviewer:
    """审校师：LLM 语义审校，只读产出工单。"""

    def __init__(self, ws: Workspace, project_id: str, llm=None) -> None:
        self.ws = ws
        self.project_id = project_id
        self.llm = llm

    def review(self, text: str, vol: int, ch: int, *, gist_text: str = "",
               memories: list[str] | None = None,
               cast_ids: list[str] | None = None) -> list[ReviewIssue]:
        if self.llm is None or not text.strip():
            return []
        prompt = REVIEW_PROMPT.format(
            bible=_bible_brief(self.ws, self.project_id, cast_ids),
            memory="\n".join(memories or []) or "（无前情）",
            gist=gist_text[:800] or "（无细纲）",
        )
        res = self.llm.complete(
            LLMRequest(messages=[LLMMessage(role="user", content=prompt + text[-3000:])],
                       # 审校预算可配：开思考时思考占预算大头（实测复杂 prompt 思考 >5K 字），
                       # 默认 800 会被吃光导致空输出→静默失效。NOVELIST_REVIEWER_TOKENS
                       # 覆盖（云端强模型给 4096，本地 9B 给 2048）。
                       max_tokens_out=int(os.environ.get("NOVELIST_REVIEWER_TOKENS", "800")),
                       temperature=0.2,
                       thinking=True)  # 判断类任务开思考（讨论）：审校 recall 优先
        )
        if res.blocked or not res.content:
            return []
        return self._parse(res.content)

    def _parse(self, content: str) -> list[ReviewIssue]:
        out: list[ReviewIssue] = []
        for line in content.splitlines():
            line = line.strip().lstrip("-•*").strip()
            if not line or line.lower() == "ok":
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 3:
                continue
            level = "block" if parts[0] == "block" else "warn"
            category = parts[1] if parts[1] in CATEGORIES else "其他"
            out.append(ReviewIssue(level=level, category=category, detail=parts[2][:200],
                                   suggestion=parts[3][:200] if len(parts) > 3 else ""))
        return out

    def review_chapter_file(self, vol: int, ch: int) -> list[ReviewIssue]:
        """直接审工作区里的某一章（chapters/ 优先，回退 drafts/）。"""
        for base in ("chapters", "drafts/chapters"):
            p = self.ws._abs(f"{self.project_id}/{base}/{vol}-{ch}.md")
            if p.exists():
                text = p.read_text(encoding="utf-8")
                gist = self.ws.outline_chapter_path(self.project_id, vol, ch)
                return self.review(text, vol, ch,
                                   gist_text=gist.read_text(encoding="utf-8") if gist.exists() else "")
        return []
