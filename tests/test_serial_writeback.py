"""串行化改造 + 事件驱动回写的冒烟测试（docs/04 §4.2 / ADR-013）。"""

from __future__ import annotations

import pytest

from novelist.core.pipeline import PipelineStateError, PipelineStateMachine
from novelist.core.session import SessionInfo
from novelist.core.writeback import LandedEvent, WritebackError, commit_event


def test_pipeline_no_memory_compile_stage():
    # 彻底移除并行批次与"记忆编纂"独立工序：状态机不含"记忆编纂"阶段，推进会失败
    st = PipelineStateMachine()
    assert st.current == "立项"
    with pytest.raises(PipelineStateError):
        st.advance("记忆编纂")  # 已从工序中移除
    # 合法路径仍可推进到审查之后
    for s in ("世界观", "大纲", "细纲", "正文", "审查", "待发布"):
        st.advance(s)
    assert st.current == "待发布"


def test_commit_event_real_time_writeback():
    ev = LandedEvent(
        project_id="proj-x", vol=2, ch=7, seq=1,
        kind="turning_point", summary="苏晚继任掌门",
        participants=["char:cz7"],
    )
    sess = SessionInfo(project_id="proj-x", agent="orchestrator")
    assert commit_event(ev, sess) is True


def test_commit_event_requires_summary():
    ev = LandedEvent(project_id="proj-x", vol=2, ch=7, seq=1, kind="conflict", summary="")
    sess = SessionInfo(project_id="proj-x", agent="orchestrator")
    with pytest.raises(WritebackError):
        commit_event(ev, sess)
