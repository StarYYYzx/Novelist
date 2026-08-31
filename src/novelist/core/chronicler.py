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
from .memory import MemoryConflictError, MemoryWriter, tokenize

EVENT_KINDS = ("conflict", "discovery", "reveal", "turning_point", "dialogue", "departure")

EXTRACT_PROMPT = """你是记忆编纂员。阅读下面这一章正文，提取其中**确实发生了**的剧情事件，
以及本章结束时人物的**状态变化**。

第一部分——事件行，每行一个事件，格式严格为（竖线分隔，第三段可省略）：
事件简述 | 类型 | 涉及人物姓名（逗号分隔）
- 类型只能是：conflict、discovery、reveal、turning_point、dialogue、departure
- 涉及人物只写正文里真实出现过的人名，不要编造
- 事件简述**直接写内容，不要用括号括起，不要用「（」「）」**

第二部分——状态行，以「状态：」开头，每行一个人物：
状态：人物名 | 修为：新境界 | 位置：新地点 | 获得：物品 | 失去：物品 | 受伤：伤情 | 痊愈：伤情
- 只写**本章内发生的变化**，没有变化的人物不要输出
- 修为只写正文明确写到突破/跌落的，不要臆测；**「借用/同步他人境界」不是修为变化，不要写**
- **人物名必须是已出场人物；修为变化只记在该人物本人名下**——与主角绑定/借用
  相关的修为波动属于主角，不要记到被借力的配角身上（实测张冠李戴：叶岚绑定
  配角的修为被记成配角跌境，R-STATE 误报）
- 获得/失去**只写具体的物品实体**（丹药、符箓、法宝、秘籍、材料等可持有之物），
  **能力、力量、权限、警告信息、关注等抽象概念不是物品，不要写**

不要输出标题、序号、解释或空行。事件最多 {max_events} 条，宁少勿多。

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
    # 世界状态变更（B-STATE）：char_id -> 本轮合并进 worldstate 的字段
    state_updates: dict[str, dict] = field(default_factory=dict)
    # 伏笔流转（暗线）：本轮 planted→active 的伏笔数
    threads_activated: int = 0

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
    def extract(self, chapter_text: str, *, max_events: int = 3, max_chars: int = 2500
                ) -> tuple[list[ExtractedEvent], list[tuple[str, dict]]]:
        """LLM 从正文抽取事件与状态变化。无 LLM 时返回空（不做假）。

        返回 (events, state_changes)；state_changes 为 [(char_id, delta)]。
        """
        if self.llm is None or not chapter_text.strip():
            return [], []
        res = self.llm.complete(
            LLMRequest(
                messages=[LLMMessage(role="user",
                                     content=EXTRACT_PROMPT.format(max_events=max_events)
                                     + chapter_text[-max_chars:])],
                max_tokens_out=450,
                temperature=0.3,
            )
        )
        if res.blocked or not res.content:
            return [], []
        return self._parse(res.content, max_events)

    def _parse(self, content: str, max_events: int) -> tuple[list[ExtractedEvent], list[tuple[str, dict]]]:
        from .worldstate import parse_state_lines

        # 状态行先摘出来，避免「状态：X | 修为：Y」被当成事件行
        state_changes = parse_state_lines(content, self._name_map)
        event_lines = [ln for ln in content.splitlines()
                       if not ln.strip().startswith(("状态：", "状态:"))]

        out: list[ExtractedEvent] = []
        for line in event_lines:
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
        return out, state_changes

    # ---- 写入（含冲突双检）----
    def _link_threads(self, events: list[ExtractedEvent], vol: int, ch: int,
                      *, payoff: bool = False) -> int:
        """暗线关联（讨论第 6 轮）：伏笔↔事件关联 + 状态流转。

        - 用伏笔 desc 的关键词（token 交集）匹配事件摘要，命中即填 affected_threads；
        - 行文期：planted → active（装上闭环：不再只是"登记 + 注入提醒"）；
        - **收尾期（`payoff=True`，第九批）**：命中事件的 active 伏笔 → `paid_off`，
          并记 `returned` 落点——"收尾期回收清单"的自动兑现判定。
        - 返回本次状态流转的伏笔数。
        """
        p = self.ws._abs(f"{self.project_id}/bible/plot_threads.json")
        if not p.exists() or not events:
            return 0
        try:
            threads = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return 0
        if not isinstance(threads, list):
            return 0

        activated = 0
        paid_off = 0
        for ev in events:
            if ev.threads:  # 已有显式关联（细纲/LLM 给出）则跳过
                continue
            ev_tokens = set(tokenize(ev.summary))
            if not ev_tokens:
                continue
            for t in threads:
                if not isinstance(t, dict) or not t.get("id"):
                    continue
                t_tokens = set(tokenize(f"{t.get('id')} {t.get('desc', '')}"))
                if t_tokens and ev_tokens & t_tokens:
                    if t["id"] not in ev.threads:
                        ev.threads.append(t["id"])
                    if t.get("status") == "planted":
                        t["status"] = "active"
                        activated += 1
                    elif payoff and t.get("status") == "active" \
                            and str(t.get("scope") or "volume") != "book":
                        # 收尾期自动回收只判**卷内线**；book 线是全书主线跨卷，
                        # 卷末事件与其关键词重合（如"血祭图谋被遏制"）不等于
                        # 主使落网——误判会把下卷钩子提前抹掉（proj-t5 实测）。
                        t["status"] = "paid_off"
                        t["returned"] = {"vol": vol, "ch": ch}
                        paid_off += 1
        if activated or paid_off:
            try:
                p.write_text(json.dumps(threads, ensure_ascii=False, indent=2),
                             encoding="utf-8")
            except OSError:  # pragma: no cover
                pass
        return activated + paid_off

    def commit(self, events: list[ExtractedEvent], vol: int, ch: int, *, project_id: str = "",
               state_changes: list[tuple[str, dict]] | None = None,
               tag: str = "c", payoff: bool = False) -> ChroniclerReport:
        """逐条写入；冲突回退并记入 report，不静默入库（docs/06 §4.4）。

        `state_changes` 非空时同步更新世界状态（B-STATE），
        并把 delta 写进对应人物经历的 `state_delta` 字段（docs/06 §3.5 预留字段）。
        `payoff=True`（收尾期，第九批）：命中事件的 active 伏笔自动判 paid_off。
        """
        report = ChroniclerReport(extracted=len(events), events=list(events))
        pid = project_id or self.project_id
        deltas_by_char: dict[str, dict] = {}
        for cid, delta in (state_changes or []):
            deltas_by_char.setdefault(cid, {}).update(delta)

        # 暗线：先做伏笔↔事件关联与状态流转，再写入（affected_threads 非空才有闭环）
        report.threads_activated = self._link_threads(events, vol, ch, payoff=payoff)

        for i, ev in enumerate(events, 1):
            try:
                self._writer.append_plot_event({
                    "id": f"ev:{pid}:{vol}:{ch}:{tag}{i}",
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
                        "state_delta": deltas_by_char.get(cid),
                    })
                except MemoryConflictError:
                    pass  # 同事件同人已入库，不重复追加
            report.written += 1

        # 世界状态（B-STATE）：硬状态层的更新与事件写入同一事务语义——
        # 先合 delta，再把实际生效的变更记进 report 供编排层/人工核对。
        if deltas_by_char:
            from .worldstate import apply_delta

            for cid, delta in deltas_by_char.items():
                applied = apply_delta(self.ws, self.project_id, cid, delta,
                                      at={"vol": vol, "ch": ch})
                if applied:
                    report.state_updates[cid] = applied
        return report

    def run(self, chapter_text: str, vol: int, ch: int, *, max_events: int = 3,
            tag: str = "c", payoff: bool = False) -> ChroniclerReport:
        """一次性完成抽取 + 双检 + 写入 + 状态更新（编排层的主要入口）。

        `tag` 用于事件 id 命名空间：事件循环逐事件调用时传 e1/e2/…，
        避免同一章内不同片段抽出的事件 id 撞名。
        `payoff`：收尾期传 True，命中事件的 active 伏笔判 paid_off（第九批）。
        """
        events, state_changes = self.extract(chapter_text, max_events=max_events)
        return self.commit(events, vol, ch, state_changes=state_changes, tag=tag,
                           payoff=payoff)
