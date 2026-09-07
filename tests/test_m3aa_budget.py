"""M3aa 批次：ADR-032 取证预算（max_rounds）可配性单测。

验证：`agentic_review` 的取证轮次上限生效可覆盖——脚本需 2 次取证后才出工单；
预算不足（max_rounds=1）→ 触顶回退单发；预算充足（max_rounds=8）→ 走 agent 路径出工单。
全部确定性 Provider，绝不真调 LLM（docs/09 §2.1）。
"""

from __future__ import annotations

from novelist.consistency.reviewer_agent import agentic_review
from novelist.core.chronicler import Chronicler
from novelist.core.chronicler_agent import agentic_chronicle
from novelist.core.session import SessionInfo
from novelist.providers.fake import FakeProvider, ScriptedProvider
from novelist.storage.workspace import Workspace


def _ws(tmp_path):
    ws = Workspace(root=str(tmp_path))
    (ws._abs("p/bible")).mkdir(parents=True, exist_ok=True)
    (ws._abs("p/bible/characters.json")).write_text('[]', encoding="utf-8")
    (ws._abs("p/outline")).mkdir(parents=True, exist_ok=True)
    return ws


def _session():
    return SessionInfo(project_id="p", agent="test")


# 脚本：先两次取证（工具自旋），第 3 次才出工单——用于验证预算是否被 clamp
_VERIFY_SCRIPT = [
    {"tool": "query_memory", "args": {"query": "玉佩"}},
    {"tool": "get_character_history", "args": {"character_id": "c1"}},
    {"final": "warn | 事实前后矛盾 | 玉佩在记忆里已毁，本章却完好 | 核对玉佩去向\n"},
]


def test_agentic_review_insufficient_budget_falls_back(tmp_path):
    """预算收紧（max_rounds=1 < 需要的 2）→ 取证到位前触顶 → 回退单发。"""
    ws = _ws(tmp_path)
    ar = agentic_review("玉佩完好正文", 1, 1, ws=ws, project_id="p",
                        provider=ScriptedProvider(_VERIFY_SCRIPT), session=_session(),
                        max_rounds=1)
    assert ar.mode == "fallback"
    # 回退到单发 FakeProvider 之外；无工单视为 0（fallback 无 LLM 输入时为空）
    # 这里不引入第二个 LLM，故 fallback 亦为空——只验证"预算不足即回退"即可


def test_agentic_review_sufficient_budget_goes_agent(tmp_path):
    """预算充足（max_rounds=8 ≥ 需要的 2）→ 走 agent 证据环并产出经取证确认的工单。"""
    ws = _ws(tmp_path)
    ar = agentic_review("玉佩完好正文", 1, 1, ws=ws, project_id="p",
                        provider=ScriptedProvider(_VERIFY_SCRIPT), session=_session(),
                        max_rounds=8)
    assert ar.mode == "agent"
    assert len(ar.issues) == 1
    assert ar.issues[0].category == "事实前后矛盾"
    # 两次取证轨迹留痕：证据环确实读到了两件证据才定论
    tools = [e["tool"] for e in ar.evidence if e["kind"] == "tool"]
    assert tools == ["query_memory", "get_character_history"]


def test_agentic_review_default_rounds_is_8(tmp_path):
    """默认预算 8：与充足用例同语义——无须用户指定即能走完 2 次取证出工单。"""
    ws = _ws(tmp_path)
    ar = agentic_review("玉佩完好正文", 1, 1, ws=ws, project_id="p",
                        provider=ScriptedProvider(_VERIFY_SCRIPT), session=_session())
    assert ar.rounds == 3 and ar.mode == "agent" and len(ar.issues) == 1


def test_agentic_chronicle_rounds_passthrough(tmp_path):
    """agentic_chronicle 的 max_rounds 透传到仲裁环；一次性给最终裁决时仍正常落库。"""
    ws = _ws(tmp_path)
    ch = Chronicler(ws, "p", llm=FakeProvider(
        reply="事件：苏晚击败墨无极 | conflict | 苏晚,墨无极\n"))
    arb_provider = ScriptedProvider([{"final": "保持：[1]\n"}])
    report, arb = agentic_chronicle(ch, "正文", provider=arb_provider,
                                    session=_session(), vol=1, ch=1, max_rounds=4)
    assert arb.rounds == 1       # 取证环 1 轮即裁决，预算未触发
    assert report.written == 1   # 被"保持"的候选照常落库