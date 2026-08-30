"""记忆编纂员（docs/05 §3 / §5.4，ADR-013，B-03）。

## 它补的是什么缺口

`produce_chapter` 原本只回写一条章级**合成事件**「完成第 X 卷第 Y 章」——不含任何情节内容。
ADR-013 要求的是「正文每个事件落定即实时回写」，F11.1 要求提炼剧情进展/人物经历/关系/
伏笔/时间推进。这两条此前**完全没有实现**，A9 验收项也无法真正满足。
端到端测试里那 29 条真实事件全部由外部脚本代做。

本模块把编纂员做成系统内的正式组件：

1. `extract()` —— 用 LLM 从成章正文里抽取"确实发生了"的情节事件；
2. `commit()`  —— 逐条走 `MemoryWriter` 的**冲突双检**（规则层：重复入库 + bible 引用完整性；
   语义层：`semantic_checker` 回调），冲突即回退并记入报告，**不静默入库**；
3. 同时为每个涉及人物追加一条人物经历（`append_experience`）。

无 LLM 时 `extract()` 返回空——编纂是 LLM 能力，不做假。编排层据此决定是否降级为
"仅章级合成事件"。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable

from .llm import LLMMessage, LLMRequest
from .memory import MemoryConflictError, MemoryWriter

EVENT_KINDS = ("conflict", "discovery", "reveal", "turning_point", "dialogue", "departure")

EXTRACT_PROMPT = """你是记忆编纂员。阅读下面这一章正文，提取其中**确实发生了**的剧情事件。

只输出事件行，每行一个事件，格式严格为（竖线分隔，第三段可省略）：
事件简述 | 类型 | 涉及人物姓名（逗号分隔）

- 类型只能是：conflict、discovery、reveal、turning_point、dialogue、departure
- 涉及人物只写正文里真实出现过的人名，不要编造
- 不要输出标题、序号、解释或空行
- 最多 {max_events} 条，宁少勿多，只写真正推动剧情的事

正文：
"""


@dataclass
class ExtractedEvent:
    summary: str
    kind: str
    participant_ids: list[str] = field(default_factory=list)
    threads: list[str] = field(default_factory=list)


@dataclass
class ChroniclerReport:
    extracted: int = 0
    written: int = 0
    conflicts: list[str] = field(default_factory=list)
    events: list[ExtractedEvent] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.extracted > 0 and not self.conflicts


class Chronicler:
    """记忆编纂员：LLM 抽取 + 冲突双检 + 写入记忆层。"""

    def __init__(
        self,
        ws,
        project_id: str,
        *,
        llm=None,
        embedding=None,
        semantic_checker: Callable[[str, list[str]], bool | None] | None = None,
    ) -> None:
        self.ws = ws
        self.project_id = project_id
        self.llm = llm
        self.embedding = embedding
        self.semantic_checker = semantic_checker
        self._writer = MemoryWriter(ws, project_id, embedding=embedding,
                                    semantic_checker=semantic_checker)
        self._name_map = self._build_name_map()

    # ---- 姓名 → 人物 id ----
    def _build_name_map(self) -> dict[str, str]:
        p = self.ws._abs(f"{self.project_id}/bible/characters.json")
        mapping: dict[str, str] = {}
        if p.exists():
            try:
                chars = json.loads(p.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                chars = []
            for c in chars if isinstance(chars, list) else []:
                if not isinstance(c, dict) or not c.get("id"):
                    continue
                for nm in [c.get("name"), *(c.get("aliases") or [])]:
                    if nm:
                        mapping[str(nm)] = c["id"]
        return mapping

    def _resolve_names(self, summary: str, declared: list[str]) -> list[str]:
        """把姓名解析成人物 id；模型漏给姓名时从简述里回扫圣经姓名。"""
        ids: list[str] = []
        for nm in declared:
            cid = self._name_map.get(nm)
            if cid and cid not in ids:
                ids.append(cid)
        for nm, cid in self._name_map.items():
            if nm and nm in summary and cid not in ids:
                ids.append(cid)
        return ids

    # ---- 抽取 ----
    def extract(self, chapter_text: str, *, max_events: int = 3, max_chars: int = 2500) -> list[ExtractedEvent]:
        """LLM 从正文抽取事件。无 LLM 时返回空列表（不做假）。"""
        if self.llm is None or not chapter_text.strip():
            return []
        res = self.llm.complete(
            LLMRequest(
                messages=[LLMMessage(role="user",
                                     content=EXTRACT_PROMPT.format(max_events=max_events)
                                     + chapter_text[-max_chars:])],
                max_tokens_out=300,
                temperature=0.3,
            )
        )
        if res.blocked or not res.content:
            return []
        return self._parse(res.content, max_events)

    def _parse(self, content: str, max_events: int) -> list[ExtractedEvent]:
        out: list[ExtractedEvent] = []
        for line in content.splitlines():
            line = line.strip().lstrip("-•*").strip()
            line = re.sub(r"^\d+[.、)．]\s*", "", line)
            if "|" not in line:
                continue
            parts = [p.strip() for p in line.split("|")]
            summary = re.sub(r"^事件[:：]\s*", "", parts[0]).strip()
            if len(summary) < 4:
                continue
            kind = parts[1] if len(parts) > 1 and parts[1] in EVENT_KINDS else "turning_point"
            names = [n.strip() for n in (parts[2].split(",") if len(parts) > 2 else []) if n.strip()]
            out.append(ExtractedEvent(
                summary=summary[:120],
                kind=kind,
                participant_ids=self._resolve_names(summary, names),
            ))
            if len(out) >= max_events:
                break
        return out

    # ---- 写入（含冲突双检）----
    def commit(self, events: list[ExtractedEvent], vol: int, ch: int, *, project_id: str = "") -> ChroniclerReport:
        """逐条写入；冲突回退并记入 report，不静默入库（docs/06 §4.4）。"""
        report = ChroniclerReport(extracted=len(events), events=list(events))
        pid = project_id or self.project_id
        for i, ev in enumerate(events, 1):
            try:
                self._writer.append_plot_event({
                    "id": f"ev:{pid}:{vol}:{ch}:c{i}",
                    "at": {"vol": vol, "ch": ch},
                    "type": ev.kind,
                    "summary": ev.summary,
                    "participants": list(ev.participant_ids),
                    "affected_threads": list(ev.threads),
                })
            except MemoryConflictError as e:
                report.conflicts.append(f"[{vol}:{ch}] {ev.summary[:30]} :: {e}")
                continue
            for cid in ev.participant_ids:
                try:
                    self._writer.append_experience(cid, {
                        "at": {"vol": vol, "ch": ch},
                        "summary": ev.summary,
                        "state_delta": None,
                    })
                except MemoryConflictError:
                    pass  # 同事件同人已入库，不重复追加
            report.written += 1
        return report

    def run(self, chapter_text: str, vol: int, ch: int, *, max_events: int = 3) -> ChroniclerReport:
        """一次性完成抽取 + 双检 + 写入（编排层的主要入口）。"""
        events = self.extract(chapter_text, max_events=max_events)
        return self.commit(events, vol, ch)
