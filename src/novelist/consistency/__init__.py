"""一致性保障（docs/04 §5.4 / docs/07 §5，ADR-009）。

规则层（确定性）见 rules.py；语义层（LLM）见 semantic.py；合并见 __init__.run_consistency。
"""

from __future__ import annotations

from ..storage.workspace import Workspace
from .rules import RuleAlert, run_rule_checks

ConsistencyAlert = RuleAlert


def run_consistency(ws: Workspace, project_id: str) -> list[ConsistencyAlert]:
    """对项目执行一致性检查：规则层 + 语义层（语义层由编排器按需调用子代理）。

    返回结构化告警（含 block/warn 级别，docs/02 F4.2）。
    """
    return run_rule_checks(ws, project_id)


__all__ = ["run_consistency", "ConsistencyAlert", "RuleAlert", "run_rule_checks"]
