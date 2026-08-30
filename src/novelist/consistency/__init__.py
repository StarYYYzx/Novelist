"""一致性保障（docs/04 §5.4 / docs/07 §5，ADR-009）。

规则层（确定性）见 `rules.py`：R-REF 引用完整性、R-TL 时间线单调、
R-LEX 正文用词纪律、R-PWR 境界表述一致性。
语义层（LLM）见 `reviewer.py`：审校师泛读章节对照圣经，产出审计工单。
"""

from __future__ import annotations

from ..storage.workspace import Workspace
from .reviewer import ReviewIssue, Reviewer
from .rules import RuleAlert, run_lexicon_checks, run_rule_checks

ConsistencyAlert = RuleAlert


def run_consistency(ws: Workspace, project_id: str, *, llm=None) -> list[ConsistencyAlert]:
    """对项目执行一致性检查（docs/02 F4.2）。

    - 总是跑确定性规则层。
    - 传入 `llm` 时追加语义层审校（审校师），把 `ReviewIssue` 折算成同一套
      `RuleAlert` 结构（rule_id = "R-SEM"），便于统一分级与展示。
    """
    alerts: list[ConsistencyAlert] = list(run_rule_checks(ws, project_id))
    if llm is not None:
        reviewer = Reviewer(ws, project_id, llm)
        # 语义检必须落到具体章节才有修订价值；chapters/ 优先，回退 drafts/
        for base in ("chapters", "drafts/chapters"):
            d = ws._abs(f"{project_id}/{base}")
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.md")):
                parts = f.stem.split("-")
                if len(parts) != 2 or not all(x.isdigit() for x in parts):
                    continue
                for issue in reviewer.review_chapter_file(int(parts[0]), int(parts[1])):
                    alerts.append(RuleAlert(
                        level=issue.level,
                        rule_id="R-SEM",
                        object_ref=f"{f.stem}｜{issue.category}",
                        detail=issue.detail + (f"（建议：{issue.suggestion}）" if issue.suggestion else ""),
                    ))
            break
    return alerts


__all__ = [
    "run_consistency",
    "ConsistencyAlert",
    "RuleAlert",
    "run_rule_checks",
    "run_lexicon_checks",
    "ReviewIssue",
    "Reviewer",
]
