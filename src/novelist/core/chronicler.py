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
from ..storage.workspace import WorkspaceError

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

第三部分——时间行与约定行（ADR-019，docs/06 §3.3）：

时间：+90日
- 表示这段正文结束后故事时间推进了多少（**相对天数**，不是历法日期）
- **每段正文必须输出 1 条**：正文明确写到时间跨度（"三日后""闭关三月"）→ 写对应
  +N日；正文没有时间流逝、多线并进 → 写「时间：同日」；回忆插叙 → 写「时间：闪回」
- **禁止编造**：正文没有时间依据时写「时间：同日」，绝不要自行加天数
- 可加备注：时间：+90日 | 叶岚闭关结束

约定：叶岚出关｜+90日
- 表示正文中角色**约定/宣告**在未来某时发生的事（闭关出关、三年之约、比试之期等）
- 竖线前写"什么事"，竖线后写"距今多少天"
- 只登记正文明确约定的，不要臆测；**正文里没有出现的承诺，即使与示例形状相似也不要写**

时间行必须输出 1 条；约定行最多 1 条，没有就不写。

第四部分——线索行（**仅当下方列出了【本书线索账本】时才输出**；账本为空则整个部分跳过）：
线索：ln:xxx | 推 | 一句话
- 只使用【本书线索账本】中列出的线索 id，**不得自造 id**
- 动作只许：开（账本 dormant 线在本章正式启动）、推（active 线剧情推进）、
  闭（该线在本章收束完结）、交织（两条线在同一事件互相作用）、反转（剧情转折）
- 本章没碰任何线索就不输出；**宁缺勿滥，不要为了交差硬凑线索行**
- 判断"这条线在本章动了没有"看剧情事实：宗门派人查探=推；主角得知内幕=推；
  线索真相揭晓并完结=闭

{lines_block}
不要输出标题、序号、解释或空行。事件最多 {max_events} 条，宁少勿多。

正文：
"""


PERSPECTIVE_PROMPT = """你是记忆编纂员。下面是刚写完的一段正文，以及本段的出场人物。
请**分别站在每个人物的立场**，用一句话记下"对他而言，刚才发生了什么"。

同一件事，不同人物的感受与判断是不同的：赢家觉得畅快，输家觉得折辱，旁观者只看到
表象——**这些差异必须写出来**，不要写成同一句客观描述。

出场人物：{names}

【输出格式】每人一行，竖线分隔，共 4 段：
人物名 | 立场情绪（≤8 字） | 视角概述（一句话，站在他的处境与判断来写） | 关系变化

【关系变化列：必须填，无变化写"无"】
- 只写"他对另一名出场人物的关系/态度变化"，格式：对<人名>：<方向>·<变化短语>；多条用"；"分隔，最多 2 条
- <方向> 只许用：升温 / 降温 / 转向 / 断裂 / 复合（其余词会被丢弃；不确定就写"无"，禁止编造变化）
- <变化短语> ≤16 字，描述"变成什么样"（如：信任加深、起了提防、当众反目），不要重复方向词
- 示例（单条即可）：对苏晚：升温·开始信任；对墨无极：降温·起了提防

【纪律】
- 只写上面列出的人物；正文里没真正出场的不要写
- 视角概述要带**他的立场**：他觉得自己赢了还是亏了、他怎么看待别人、他接下来想什么
- 不要写"某某做了某事"这种旁观者口吻的复述
- 不要输出标题、序号、解释或空行

正文：
"""


def _chapter_window(text: str, max_chars: int, *, head_chars: int = 800) -> str:
    """章级抽取输入窗口：头 + 尾（H2 修复，2026-09-05）。

    原 tail-only 截断（`text[-max_chars:]`）会漏掉章头的事件/状态/**时间行**——
    时间行是 worldstate.time 推进的唯一来源，漏抽即本章时间静默停走。
    头部固定保留前 head_chars 字（开篇事件与时间锚点密度最高），其余预算给尾部。

    实现已上移到 `core/prompt_budget.head_tail_window`（批次 2：审校侧踩了同一个坑，
    两处各写一份正是坑的成因）。此处保留薄封装，调用点与既有测试零改动。
    """
    from .prompt_budget import head_tail_window

    return head_tail_window(text, max_chars, head_chars=head_chars)


@dataclass
class ExtractedEvent:
    summary: str
    kind: str
    participant_ids: list[str] = field(default_factory=list)
    threads: list[str] = field(default_factory=list)


@dataclass
class Extraction:
    """一次抽取的全部结果（事件 + 状态 + 时间 + 约定）。

    原先 `extract()` 返回 `(events, state_changes)` 二元组；ADR-019 需要再带出
    时间行与约定行，二元组会一路改成四元组——改成具名容器，后续再加字段不改签名。
    """

    events: list[ExtractedEvent] = field(default_factory=list)
    state_changes: list[tuple[str, dict]] = field(default_factory=list)
    time_lines: list[str] = field(default_factory=list)      # 「时间：+90日（| 备注）」原文
    pending_lines: list[str] = field(default_factory=list)   # 「约定：叶岚出关｜+90日」原文
    line_rows: list[dict] = field(default_factory=list)      # ADR-025：「线索：ln:x | 推 | …」解析结果


@dataclass
class ChroniclerReport:
    extracted: int = 0
    written: int = 0
    conflicts: list[str] = field(default_factory=list)
    events: list[ExtractedEvent] = field(default_factory=list)
    # P0-C 入库闸门：被拦下未写入的事件（理由随行披露）
    rejected: list[str] = field(default_factory=list)
    # 世界状态变更（B-STATE）：char_id -> 本轮合并进 worldstate 的字段
    state_updates: dict[str, dict] = field(default_factory=dict)
    # 伏笔流转（暗线）：本轮 planted→active 的伏笔数
    threads_activated: int = 0
    # ADR-025：线索账本回写摘要（变更/计划外闭合候选/新提名 pending）
    lines_changed: list[str] = field(default_factory=list)
    line_candidates: list[str] = field(default_factory=list)
    lines_pending: list[str] = field(default_factory=list)
    # 时间轴（ADR-019）：本轮推进的天数（0 = 未推进/闪回）
    time_advanced: int = 0
    # 时间轴：本轮登记的定时事件 [(pending_id, what, due)]
    pending_added: list[tuple[str, str, int]] = field(default_factory=list)
    # 时间轴：解析告警（无法识别的时间增量、超上限的天数等）
    warnings: list[str] = field(default_factory=list)

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

    def _build_lines_block(self) -> str:
        """线索账本注入块（ADR-025）：账本为空/全闭合 → 空串（prompt 要求跳过线索行）。"""
        from .lines import load_lines

        try:
            ledger = load_lines(self.ws, self.project_id)
        except Exception:  # noqa: BLE001
            return ""
        live = [ln for ln in ledger
                if ln.get("status") != "closed" and not ln.get("closed")]
        if not live:
            return ""
        rev = {v: k for k, v in self._name_map.items()}
        rows = []
        for ln in live[:8]:
            members = "、".join(rev.get(str(m), str(m))
                                for m in (ln.get("members") or [])[:4] if m)
            rows.append(f"- {ln.get('id')}（{ln.get('kind')}·{ln.get('status')}"
                        + (f"；成员：{members}" if members else "")
                        + f"）：{str(ln.get('desc') or '')[:40]}")
        return "【本书线索账本】（线索行只准引用下列 id）：\n" + "\n".join(rows)

    # ---- 抽取 ----
    def extract(self, chapter_text: str, *, max_events: int = 3, max_chars: int = 2500
                ) -> Extraction:
        """LLM 从正文抽取事件、状态变化、时间推进、约定与线索行（ADR-019 / ADR-025）。

        无 LLM 时返回空 `Extraction`（不做假）。时间/约定/线索行搭在同一次抽取调用里，
        **不额外增加 LLM 调用**；账本为空时 prompt 不要求线索行（行为与旧版一致）。
        """
        if self.llm is None or not chapter_text.strip():
            return Extraction()
        res = self.llm.complete(
            LLMRequest(
                messages=[LLMMessage(role="user",
                                     content=EXTRACT_PROMPT.format(
                                         max_events=max_events,
                                         lines_block=self._build_lines_block())
                                     + _chapter_window(chapter_text, max_chars))],
                max_tokens_out=450,
                temperature=0.3,
                thinking=True,  # 判断类：事件/状态/时间/线索结构化抽取，开思考
            )
        )
        if res.blocked or not res.content:
            return Extraction()
        return self._parse(res.content, max_events)

    def _parse(self, content: str, max_events: int) -> Extraction:
        from . import worldstate
        from .lines import parse_line_rows
        from .timeline import _PENDING_LINE_RE, _TIME_LINE_RE

        # 状态/时间/约定/线索行先摘出来，避免被当成事件行
        state_changes = worldstate.parse_state_lines(content, self._name_map)
        time_lines: list[str] = []
        pending_lines: list[str] = []
        line_rows = parse_line_rows(content)
        event_lines: list[str] = []
        for ln in content.splitlines():
            s = ln.strip()
            if _TIME_LINE_RE.match(s):
                if len(time_lines) < 1:      # 单次抽取最多 1 条（逐事件结算：推进可跨多次抽取累计）
                    time_lines.append(s)
            elif _PENDING_LINE_RE.match(s):
                if len(pending_lines) < 1:
                    pending_lines.append(s)
            elif s.startswith(("状态：", "状态:")):
                continue
            elif s.startswith(("线索：", "线索:")):
                continue                     # 线索行由 parse_line_rows 统一解析
            else:
                event_lines.append(ln)

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
            # 质量闸门（2026-09-04 实证：「目标出现」4 字压线入库，无参与者无线索，
            # 对 RAG 是纯噪声）：无参与者的事件须摘要 ≥8 字才有最低信息量。
            if not names and len(summary) < 8:
                continue
            out.append(ExtractedEvent(
                summary=summary[:120],
                kind=kind,
                participant_ids=self._resolve_names(summary, names),
            ))
            if len(out) >= max_events:
                break
        return Extraction(events=out, state_changes=state_changes,
                          time_lines=time_lines, pending_lines=pending_lines,
                          line_rows=line_rows)

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
        # 2026-09-19 审计修复：读写统一走 Workspace 通道（write_json 原子写，
        # 此前 write_text 直写，中断即半截文件——伏笔账本损坏会让后续章节注入崩）
        if not events:
            return 0
        p = self.ws.bible_path(self.project_id, "plot_threads")
        if not p.exists():
            return 0
        try:
            threads = self.ws.read_json(self.project_id, p)
        except (ValueError, OSError, WorkspaceError):
            return 0
        if not isinstance(threads, list):
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
                self.ws.write_json(p, threads)  # 原子写（tmp + replace，ADR-016）
            except OSError:  # pragma: no cover
                pass
        return activated + paid_off

    def commit(self, events: list[ExtractedEvent], vol: int, ch: int, *, project_id: str = "",
               state_changes: list[tuple[str, dict]] | None = None,
               tag: str = "c", payoff: bool = False,
               time_lines: list[str] | None = None,
               pending_lines: list[str] | None = None,
               line_rows: list[dict] | None = None) -> ChroniclerReport:
        """逐条写入；冲突回退并记入 report，不静默入库（docs/06 §4.4）。

        `state_changes` 非空时同步更新世界状态（B-STATE），
        并把 delta 写进对应人物经历的 `state_delta` 字段（docs/06 §3.5 预留字段）。
        `payoff=True`（收尾期，第九批）：命中事件的 active 伏笔自动判 paid_off。
        `time_lines` / `pending_lines`（ADR-019）：推进故事时间、登记定时事件。
        `line_rows`（ADR-025）：线索行回写账本（开/推/闭/交织/反转 + 计划外闭合
        candidate + 账本外 id 提名 pending），确定性校验，任何失败降级不阻断。
        """
        report = ChroniclerReport(extracted=len(events), events=list(events))
        pid = project_id or self.project_id
        deltas_by_char: dict[str, dict] = {}
        for cid, delta in (state_changes or []):
            deltas_by_char.setdefault(cid, {}).update(delta)
        deltas_by_char = dict(self._gate_state(list(deltas_by_char.items()), report))

        # P0-C 闸门 1+3：地点名册核对 + 近似事件去重（确定性，宁缺勿错记）
        registered = self._register_terms()
        gated: list[ExtractedEvent] = []
        for ev in events:
            reason = self._gate_location(ev.summary, registered)
            if reason:
                report.rejected.append(f"[{vol}:{ch}] {ev.summary[:30]} :: {reason}")
                continue
            gated.append(ev)
        gated, dup_warns = self._gate_near_dup(gated)
        for w in dup_warns:
            report.rejected.append(f"[{vol}:{ch}] {w}")
        events = gated

        # 暗线：先做伏笔↔事件关联与状态流转，再写入（affected_threads 非空才有闭环）
        report.threads_activated = self._link_threads(events, vol, ch, payoff=payoff)

        # 线索账本回写（ADR-025）：实时落账（ADR-013 事件落定即回写，不等章末）
        if line_rows:
            try:
                from .lines import apply_extracted_rows, load_lines as _load_ledger

                _res = apply_extracted_rows(self.ws, pid, _load_ledger(self.ws, pid),
                                            line_rows, vol, ch)
                report.lines_changed = _res.get("changed") or []
                report.line_candidates = _res.get("candidates") or []
                report.lines_pending = _res.get("pending") or []
                report.warnings.extend(_res.get("warnings") or [])
            except Exception as e:  # noqa: BLE001 - 账本回写失败降级，绝不阻断编纂
                report.warnings.append(f"线索账本回写失败（{type(e).__name__}），已跳过")

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
        # 时间轴（ADR-019）：**登记先于推进**——约定行按推进前的 now 算 due；
        # 状态历史打的 t 是**章末**时刻（推进后），因为状态变更落定在本章结尾。
        self._apply_pending(pending_lines, vol, ch, report)
        self._apply_time(time_lines, vol, ch, report)
        if deltas_by_char:
            from . import worldstate as wsmod
            from .timeline import now_of
            from .worldstate import apply_delta

            end_t = now_of(wsmod.load(self.ws, self.project_id))
            for cid, delta in deltas_by_char.items():
                applied = apply_delta(self.ws, self.project_id, cid, delta,
                                      at={"vol": vol, "ch": ch, "t": end_t})
                if applied:
                    report.state_updates[cid] = applied
        return report

    # ---- P0-C 入库闸门（确定性，零调用）----

    # 强"成段地名"后缀：正常叙事散文里几乎只在**命名一个地点**时出现；
    # 未登记却命中 → 大概率是模型张冠李戴/凭空造地（v7 实测："外山打斗"被抽成
    # "鬼面跌出秘境"）。泛用方位词（大殿/山下/城中）不在此列，防误伤。
    _LOCATION_STRONG = ("秘境", "洞府", "坊市", "禁地", "遗址", "遗迹", "幻境",
                        "地宫", "皇陵", "墓穴", "秘府", "福地", "洞天")

    def _register_terms(self) -> set[str]:
        """全名册词集（characters/locations/items/skills/settings + 别名），地点闸门白名单。"""
        import json as _json

        terms: set[str] = set()
        for rel in ("characters.json", "locations.json", "items.json",
                    "skills.json", "settings.json"):
            p = self.ws._abs(f"{self.project_id}/bible/{rel}")
            try:
                data = _json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
            except (ValueError, OSError):
                data = None
            for e in data if isinstance(data, list) else []:
                if isinstance(e, dict):
                    for k in ("name", "term"):
                        if e.get(k):
                            terms.add(str(e[k]))
                    for a in (e.get("aliases") or []) + (e.get("keywords") or []):
                        if a:
                            terms.add(str(a))
        return terms

    def _gate_location(self, summary: str, registered: set[str]) -> str | None:
        """地点名册闸门：summary 命中强地名后缀且不在名册 → 拒收理由；否则 None。"""
        import re as _re

        for m in _re.finditer(r"[\u4e00-\u9fa5]{0,4}(?:" + "|".join(self._LOCATION_STRONG) + r")",
                              summary):
            term = m.group(0)
            # 剥掉前缀粘带（「出秘境」→「秘境」）：从右往左找名册精确命中，找不到再判
            stripped = term
            while stripped and stripped not in registered:
                stripped = stripped[1:]
            if stripped and stripped in registered:
                continue  # 前缀剥掉后命中名册 → 是登记地点的变体引用
            return f"地点未登记：summary 提及「{term}」但 locations 名册无此（宁缺勿错记）"
        return None

    def _gate_near_dup(self, events: list[ExtractedEvent]) -> tuple[list[ExtractedEvent], list[str]]:
        """近似事件去重：与既有 plot_event 摘要字符二元组 Jaccard ≥0.45 且有共同参与者 → 丢。

        阈值 0.45：换皮重述（"击败"→"打败了"）二元组重合约 0.45-0.6，而正常不同事件
        远低于此。确定性近似，不需要 embedding。
        """
        import re as _re

        def _bigrams(s: str) -> set[str]:
            s = _re.sub(r"[\s，。！？；：、“”‘’（）()\-—]", "", s)
            return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}

        path = self.ws._abs(f"{self.project_id}/memory/plot_events.json")
        try:
            existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        except (ValueError, OSError):
            existing = []
        existing = existing if isinstance(existing, list) else []
        kept, warns = [], []
        for ev in events:
            bg = _bigrams(ev.summary)
            dup = None
            for old in existing:
                if not isinstance(old, dict):
                    continue
                old_bg = _bigrams(str(old.get("summary") or ""))
                sim = len(bg & old_bg) / max(len(bg | old_bg), 1)
                overlap = set(ev.participant_ids) & set(old.get("participants") or [])
                if sim >= 0.45 and overlap:
                    dup = f"{str(old.get('summary'))[:24]}…(sim={sim:.2f})"
                    break
            if dup:
                warns.append(f"近似事件去重：与既有事件 {dup} 高度相似，未入库")
            else:
                kept.append(ev)
        return kept, warns

    def _gate_state(self, state_changes: list[tuple[str, dict]],
                    report: "ChroniclerReport") -> list[tuple[str, dict]]:
        """境界变更与 R-STATE 对齐：realm 值必须能按 worldview 境界表解析，否则丢弃该字段。

        v7 实测"绑定临时修为当真实 realm / 编造境界"直接进 worldstate——这里只拦
        **体系外**表述（parse_realm 返回 None），体系内的涨跌仍由 R-STATE 单调性校验。
        """
        from .worldstate import parse_realm

        try:
            wv = json.loads(self.ws._abs(f"{self.project_id}/bible/worldview.json")
                            .read_text(encoding="utf-8"))
            levels = [str(x) for x in ((wv.get("power_system") or {}).get("levels") or [])]
        except (ValueError, OSError, AttributeError):
            levels = []
        if not levels:
            return state_changes  # 无境界表 → 无从核对，放行（初始化期允许）
        out: list[tuple[str, dict]] = []
        for cid, delta in state_changes or []:
            d = dict(delta)
            for key in list(d):
                if key not in ("修为", "境界", "实力", "战力", "等级", "realm"):
                    continue
                if parse_realm(str(d[key]), levels) is None:
                    report.warnings.append(
                        f"境界变更不入档：{cid} 的「{d[key]}」不在境界体系内（{ '、'.join(levels) }）")
                    del d[key]
            if d:
                out.append((cid, d))
        return out

    # ---- 时间轴（ADR-019）----
    def _apply_pending(self, pending_lines: list[str] | None, vol: int, ch: int,
                       report: ChroniclerReport) -> None:
        """登记「约定：」行（按推进前的 now 算 due——登记先于推进）。

        去重（实测 3080ti 批量：模型照 few-shot 形状重复输出「出关」约定，
        每章堆积同类 pending，逾期后连环软 block 卡死后续章节）：
        把 what 剥掉姓名前缀归一化成"动作核"，已存在 scheduled/fired 的同核约定
        不再重复登记。
        """
        from . import worldstate as wsmod
        from .timeline import add_pending, parse_pending_line, pending_of, resolve_who

        def _edit1(a: str, b: str) -> bool:
            """同长且恰好 1 个字符不同（近形错名判定：叶蓝 vs 叶岚）。"""
            if len(a) != len(b):
                return False
            return sum(x != y for x, y in zip(a, b)) == 1

        def _key(what: str) -> str:
            """归一化动作核：剥离姓名前缀（长名优先）。

            表内无此名时回退 1 字编辑距离（实测幽灵名「叶蓝」vs 正名「叶岚」
            同长近形，模型照旧示例写错名时也能归并到同一动作核）。
            """
            text = what.strip()
            names = sorted(self._name_map, key=len, reverse=True)
            for nm in names:
                if text.startswith(nm):
                    return text[len(nm):].strip()
            for nm in names:
                if (len(nm) >= 2 and text.startswith(nm[:1])
                        and len(text) > len(nm) and _edit1(text[:len(nm)], nm)):
                    return text[len(nm):].strip()
            return text

        state = wsmod.load(self.ws, self.project_id)
        # 去重范围：**仅本章**（ADR-019 绝对锚定语义——跨章同文约定各自锚定，
        # 见 test_ingest_preserves_chronicler_timeline；3080ti 事故里同章 2-3 条
        # 才是堆积主因：每事件编纂各抽一次「出关」，本章重复即合并）
        existing = {_key(str(p.get("what") or "")) for p in pending_of(state)
                    if p.get("status") in ("scheduled", "fired")
                    and (p.get("created_at") or {}).get("vol") == vol
                    and (p.get("created_at") or {}).get("ch") == ch}
        for raw in pending_lines or []:
            parsed = parse_pending_line(raw)
            if parsed is None:
                report.warnings.append(f"约定行无法解析：{raw[:40]}")
                continue
            what, dt, warn = parsed
            if warn:
                report.warnings.append(warn)
            if dt > 3650:  # MAX_DT_WARN
                report.warnings.append(f"约定「{what[:20]}」跨度 {dt} 天，疑似抽取错误")
            key = _key(what)
            if key in existing:
                report.warnings.append(f"约定「{what[:20]}」与已有日程重复，跳过登记")
                continue
            existing.add(key)
            who = resolve_who(what, self._name_map)
            item = add_pending(self.ws, self.project_id, what=what, dt=dt, vol=vol, ch=ch,
                               who=who, unavailable_states=wsmod.unavailable_keywords(
                                   self.ws, self.project_id))
            report.pending_added.append((str(item["id"]), what, int(item["due"])))

    def _apply_time(self, time_lines: list[str] | None, vol: int, ch: int,
                    report: ChroniclerReport) -> None:
        """推进「时间：」行（取最大跨度；闪回/同日不推进但登记时点）。"""
        from .timeline import MAX_DT_WARN, advance, parse_time_line

        for raw in time_lines or []:
            dt, note, warn = parse_time_line(raw)
            if warn:
                report.warnings.append(warn)
            if dt is None:
                continue  # 闪回/无法解析：不推进，也不污染 timeline
            if dt > MAX_DT_WARN:
                report.warnings.append(f"时间增量 {dt} 天超过上限 {MAX_DT_WARN}，疑似抽取错误")
            advance(self.ws, self.project_id, dt, vol=vol, ch=ch,
                    event=note or (f"时间推进 {dt} 日" if dt else "同日内推进"))
            report.time_advanced += dt

    # ---- 角色视角记忆（ADR-020 决策四）----
    def record_perspectives(self, text: str, cast_names: list[str], vol: int, ch: int,
                            *, event_index: int = 0, max_tokens: int = 600,
                            project_id: str = "") -> int:
        """为每个出场人物补一条**角色视角**经历（每事件 +1 次 LLM 调用）。

        既有 `commit()` 把同一条事件摘要复制给所有参与者——李慕白和陈松拿到的是
        同一句"客观"描述，等于没有视角。本方法一次调用产出**全部角色的视角条目**，
        天然形成差异（同一件事，各人看法不同）。

        写入 `memory/character_histories/<id>.json`（ADR-011：经历属"实然"，
        **不写回 `bible/characters.json`**）。失败返回 0，不抛异常。
        """
        if self.llm is None or not (text or "").strip() or not cast_names:
            return 0
        names = [str(n).strip() for n in cast_names if str(n).strip()][:6]
        if not names:
            return 0
        try:
            res = self.llm.complete(
                LLMRequest(
                    messages=[LLMMessage(role="user",
                                         content=PERSPECTIVE_PROMPT.format(
                                             names="、".join(names))
                                         + text[-2500:])],
                    max_tokens_out=max_tokens,
                    temperature=0.4,
                    thinking=True,  # 判断类：视角记忆结构化归纳，开思考
                )
            )
        except Exception:  # noqa: BLE001 - 视角抽取失败不阻断生成
            return 0
        if res.blocked or not (res.content or "").strip():
            return 0

        try:
            from . import worldstate as wsmod
            from .timeline import now_of

            now_t = now_of(wsmod.load(self.ws, self.project_id))
        except Exception:  # noqa: BLE001
            now_t = 0

        pid = project_id or self.project_id
        written = 0
        # dp-intent：随视角把人物当前欲望/计划双写入记忆（id→(intent,plan) 懒加载一次）。
        # 卡上欲望是权威字段；此处按"此刻写视角时的卡现状"做快照，随事件演进留痕。
        _want_map: dict[str, tuple[str, str]] = {}
        try:
            from . import director as _d
            for _c in _d.load_characters(self.ws, self.project_id):
                _i = _c.get("intent") or ""
                _p = _c.get("plan") or ""
                if _i or _p:
                    _want_map[_c.get("id")] = (str(_i)[:160], str(_p)[:160])
        except Exception:  # noqa: BLE001 - 欲望快照失败不影响视角写入
            _want_map = {}
        for ln in (res.content or "").splitlines():
            s = ln.strip().lstrip("-•*").strip()
            s = re.sub(r"^\d+[.、)．]\s*", "", s)
            if "|" not in s:
                continue
            parts = [p.strip() for p in s.split("|")]
            name = parts[0]
            cid = self._name_map.get(name)
            if not cid:
                continue
            stance = parts[1][:20] if len(parts) > 1 else ""
            perspective = parts[2] if len(parts) > 2 else ""
            if len(perspective) < 4:
                continue
            relations: list[dict] = []
            if len(parts) > 3 and parts[3]:
                for item in re.split(r"[；;]", parts[3]):
                    # 可带"对"前缀（PERSPECTIVE_PROMPT 示例形态），也兼容裸名
                    m = re.match(r"^对?\s*(.*?)[：:](.*)$", item)
                    if not m:
                        continue
                    delta = m.group(2).strip()[:30]
                    if not delta or delta in ("无", "不变"):
                        continue  # "无变化"不产生账本增量
                    other = self._name_map.get(m.group(1).strip())
                    if other:
                        relations.append({"who": other, "delta": delta})
            entry = {
                "at": {"vol": vol, "ch": ch, "t": now_t},
                "kind": "perspective",
                "stance": stance,
                "perspective": perspective[:160],
                "relations": relations,
                "event_ref": f"ev:{pid}:{vol}:{ch}:e{event_index}",
                # summary 是既有字段（append_experience 必填 & 索引文本）：
                # 加 [视角] 前缀，与事件摘要区分，也避免与 commit 写入的条目撞 sig。
                "summary": f"[视角] {perspective[:160]}",
            }
            if cid in _want_map:
                _i, _p = _want_map[cid]
                if _i:
                    entry["intent"] = _i   # dp-intent：写视角时刻的人物欲望快照
                if _p:
                    entry["plan"] = _p     # dp-intent：写视角时刻的人物近段计划
            try:
                self._writer.append_experience(cid, entry)
                written += 1
            except MemoryConflictError:
                continue  # 同事件同人已入库
            except Exception:  # noqa: BLE001
                continue
        return written

    def run(self, chapter_text: str, vol: int, ch: int, *, max_events: int = 3,
            tag: str = "c", payoff: bool = False) -> ChroniclerReport:
        """一次性完成抽取 + 双检 + 写入 + 状态更新（编排层的主要入口）。

        `tag` 用于事件 id 命名空间：事件循环逐事件调用时传 e1/e2/…，
        避免同一章内不同片段抽出的事件 id 撞名。
        `payoff`：收尾期传 True，命中事件的 active 伏笔判 paid_off（第九批）。
        """
        ex = self.extract(chapter_text, max_events=max_events)
        return self.commit(ex.events, vol, ch, state_changes=ex.state_changes, tag=tag,
                           payoff=payoff, time_lines=ex.time_lines,
                           pending_lines=ex.pending_lines, line_rows=ex.line_rows)
