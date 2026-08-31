"""分阶段工作流（人工审查第九批·用户设想，2026-08-31 拍板）。

## 为什么需要这个模块

v5 实测（`_harness/probe_stages.py` 扫 20 章正文）证实：统一生成流程只适配
"设定引入完毕后的行文期"——

- 开篇 ch1–4 每章新实体均值 7.75（ch1 达 15 个，细纲只声明 3 人），
  该从容展开时篇幅反而最短（3637 字）；
- 行文 ch5–15 均值 1.09，稳定；
- **收尾 ch16–20 均值 0，5 条伏笔全部 active、0 条 paid_off**——一条暗线都没回收。

本模块把「开篇 / 行文 / 收尾」做成**一条主干 + PhasePolicy**，不做三套并行流程：

- **阶段判定是确定性规则，不调 LLM**（可解释可复现）：
  收尾期 = 卷剩余章数 ≤ tail_chapters；开篇期 = 卷内章号 ≤ opening_chapters
  或实体池 established 占比 < opening_fill_ratio；其余行文期。
  收尾优先于开篇（硬时间约束）。
- 差异只有四个可参数化的维度：实体配额、篇幅系数、设定分批、校验规则强度。

## 粒度口径（用户 2026-08-31 确认）

"收尾"指**卷级**（volume 的 chapter_range 走完前的最后几章），不是全书、
不是单章、不是单事件。开篇同理**每卷重算**——新卷开头读者也需要被重新引入。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum


class Phase(str, Enum):
    OPENING = "opening"
    WRITING = "writing"
    TAIL = "tail"


# ---------------------------------------------------------------------------
# 卷上下文：运行时的"卷"概念（此前 orchestrator 只有 vol 透传参数，无卷级推理）
# ---------------------------------------------------------------------------

@dataclass
class VolumeContext:
    """当前卷的确定性信息（来自 outline/volumes.json，缺失时给保守默认）。"""

    vol: int
    start: int = 1            # chapter_range 起点（卷内第一章全局章号）
    end: int = 0              # chapter_range 终点；0 = 未知（永不当收尾期）
    order: int = 1            # 全书第几卷
    summary: str = ""
    threads_to_payoff: list[str] = field(default_factory=list)  # 卷大纲声明本卷须回收的伏笔 id

    @classmethod
    def load(cls, ws, project_id: str, vol: int) -> "VolumeContext":
        entries = []
        p = ws._abs(f"{project_id}/outline/volumes.json")
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    entries = [v for v in raw if isinstance(v, dict)]
            except (ValueError, OSError):
                entries = []
        entry = next((v for v in entries
                      if int(v.get("vol", 0) or 0) == vol
                      or int(v.get("order", 0) or 0) == vol), None)
        if not entry:
            return cls(vol=vol)
        rng = entry.get("chapter_range") or [1, 0]
        try:
            start, end = int(rng[0]), int(rng[1])
        except (TypeError, ValueError, IndexError):
            start, end = 1, 0
        return cls(
            vol=vol, start=start, end=end,
            order=int(entry.get("order", vol) or vol),
            summary=str(entry.get("summary") or ""),
            threads_to_payoff=[str(t) for t in (entry.get("threads_to_payoff") or [])],
        )

    # ---- 卷内坐标 ----
    def ch_in_volume(self, ch: int) -> int:
        """卷内章号（1 起）；chapter_range 未知时退回全局章号。"""
        return ch - self.start + 1 if self.end else ch

    def chapters_left(self, ch: int) -> int:
        """含本章在内还剩几章走完本卷；end 未知时返回大数（视为永不到收尾）。"""
        return (self.end - ch + 1) if self.end else 10 ** 6

    def total_chapters(self) -> int:
        return (self.end - self.start + 1) if self.end else 0


# ---------------------------------------------------------------------------
# PhasePolicy：判定 + 阶段差异参数
# ---------------------------------------------------------------------------

@dataclass
class PhasePolicy:
    """阶段判定与差异参数（全部确定性，无 LLM）。

    可通过 worldview.json 的 `phase_policy` 键覆盖默认值（dict 形式）。
    """

    opening_chapters: int = 2        # 卷内前 N 章视为开篇
    tail_chapters: int = 2           # 卷末 K 章视为收尾
    opening_fill_ratio: float = 0.6  # established 占比低于此 → 开篇（兜底判据）
    opening_quota: int = 3           # 开篇期每章新实体配额（超出推迟）
    opening_settings_max: int = 3    # 开篇期设定分批：每事件最多注入条数
    opening_length_factor: float = 1.3   # 开篇期生成预算放大系数
    tail_length_factor: float = 1.0      # 收尾期系数（篇幅按回收清单定，不设下限）

    @classmethod
    def load(cls, ws, project_id: str) -> "PhasePolicy":
        pol = cls()
        p = ws._abs(f"{project_id}/bible/worldview.json")
        if p.exists():
            try:
                wv = json.loads(p.read_text(encoding="utf-8"))
                conf = wv.get("phase_policy") if isinstance(wv, dict) else None
                if isinstance(conf, dict):
                    for k in vars(pol):
                        if k in conf:
                            setattr(pol, k, type(getattr(pol, k))(conf[k]))
            except (ValueError, OSError, TypeError):
                pass
        return pol

    # ---- 判定 ----
    def judge(self, vctx: VolumeContext, ch: int,
              established_ratio: float | None = None) -> tuple[Phase, str]:
        """返回 (阶段, 判定依据)。收尾优先（硬时间约束），开篇次之。

        `established_ratio`：实体池 established 占比（0–1）；None 时只按章号判定。
        """
        left = vctx.chapters_left(ch)
        if vctx.end and left <= self.tail_chapters:
            return Phase.TAIL, f"卷剩余 {left} 章 ≤ tail_chapters={self.tail_chapters}"
        in_vol = vctx.ch_in_volume(ch)
        if in_vol <= self.opening_chapters:
            return (Phase.OPENING,
                    f"卷内第 {in_vol} 章 ≤ opening_chapters={self.opening_chapters}")
        if established_ratio is not None and established_ratio < self.opening_fill_ratio:
            return (Phase.OPENING,
                    f"established 占比 {established_ratio:.2f} < {self.opening_fill_ratio}")
        return Phase.WRITING, "默认行文期"

    # ---- 阶段差异 ----
    def generation_tokens(self, base: int, phase: Phase) -> int:
        factor = {Phase.OPENING: self.opening_length_factor,
                  Phase.TAIL: self.tail_length_factor}.get(phase, 1.0)
        return max(400, int(base * factor))


def established_ratio(tracker) -> float | None:
    """实体池 established 占比；无可统计实体时返回 None（不参与判定）。"""
    if tracker is None or not tracker.entities:
        return None
    from .entity import _STAGE_RANK

    total = len(tracker.entities)
    est = sum(1 for e in tracker.entities.values()
              if _STAGE_RANK[e.stage] >= _STAGE_RANK["established"])
    return est / total if total else None


# ---------------------------------------------------------------------------
# 收尾期回收清单（"收尾"缺口的核心：先知道该收什么，才谈得上判定回收）
# ---------------------------------------------------------------------------

def payoff_checklist(ws, project_id: str, vol: int, ch: int,
                     tracker=None) -> dict:
    """卷级回收清单：未回收伏笔 + 过久未出场实体。

    伏笔筛选规则（对旧数据宽容——v5 的 plot_threads 无 scope 字段）：
    - scope == "book"（全书主线跨卷）：不进卷末回收清单；
    - scope == "volume"（卷内线）或缺省：target_vol == 当前卷即进清单
      （target_vol 缺省 = planted.vol；planted 也缺省则视为当前卷）。

    返回 {"threads": [...], "dormant": [...]}。
    """
    threads_out: list[dict] = []
    p = ws._abs(f"{project_id}/bible/plot_threads.json")
    if p.exists():
        try:
            threads = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            threads = []
        for t in threads if isinstance(threads, list) else []:
            if not isinstance(t, dict) or not t.get("id"):
                continue
            if t.get("status") in ("paid_off", "returned", "unplanned"):
                continue
            scope = str(t.get("scope") or "volume")
            if scope == "book":
                continue
            target = int(t.get("target_vol") or (t.get("planted") or {}).get("vol") or vol)
            if target != vol:
                continue
            threads_out.append(t)
    dormant: list[str] = []
    if tracker is not None:
        try:
            dormant = tracker.dormant_since(ch, min_gap=5)
        except Exception:  # noqa: BLE001
            dormant = []
    return {"threads": threads_out, "dormant": dormant}


def payoff_prompt_lines(checklist: dict, *, is_final: bool = False) -> list[str]:
    """回收清单 → 生成 prompt 注入行。is_final（卷末章）语气加重。"""
    lines: list[str] = []
    threads = checklist.get("threads") or []
    if threads:
        head = ("【回收清单】本章必须实质推进以下暗线的回收"
                + ("（本卷最后一章：必须给出明确交代，不得再开新钩子）"
                   if is_final else "（临近卷末，必须开始兑现）") + "：")
        lines.append(head)
        for t in threads:
            lines.append(f"- {t.get('id')}：{t.get('desc', '')}（当前状态：{t.get('status')}）")
    dormant = checklist.get("dormant") or []
    if dormant:
        lines.append("【久未出场】以下人物/设定若合乎情节，可安排收束性出场：")
        lines.extend(f"- {d}" for d in dormant[:5])
    return lines
