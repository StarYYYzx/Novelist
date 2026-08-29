"""流水线工序状态机（docs/06 §4.1）。

立项 → 世界观 → 大纲 → 细纲 → 正文 → 审查 → 记忆编纂 → 待发布，带修订回流。
状态持久在 project.json.pipeline_state（schemas/project.schema.json）。
"""

from __future__ import annotations

PIPELINE_STAGES = ["立项", "世界观", "大纲", "细纲", "正文", "审查", "记忆编纂", "待发布", "已完成"]
REVISION_STAGE = "修订"


class PipelineStateError(Exception):
    pass


class PipelineStateMachine:
    """工序状态机；允许人工暂停/编辑/续跑与修订回流（docs/02 F1.1/F1.3）。"""

    def __init__(self, stages: list[str] | None = None) -> None:
        self._stages = stages or list(PIPELINE_STAGES)
        self._index: dict[str, int] = {s: i for i, s in enumerate(self._stages)}
        self._current = self._stages[0]

    @property
    def current(self) -> str:
        return self._current

    def can_advance(self, to_stage: str) -> bool:
        return to_stage in self._index and self._index[to_stage] > self._index[self._current]

    def advance(self, to_stage: str) -> None:
        if not self.can_advance(to_stage):
            raise PipelineStateError(f"cannot advance from {self._current} to {to_stage}")
        self._current = to_stage

    def revert_to_revision(self) -> None:
        self._current = REVISION_STAGE

    def resume_from_revision(self, target: str) -> None:
        if target not in self._index:
            raise PipelineStateError(f"unknown stage {target}")
        self._current = target

    def to_dict(self) -> str:
        return self._current
