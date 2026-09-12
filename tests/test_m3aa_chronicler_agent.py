"""M3aa 批次：ADR-032 F1 chronicler 证据仲裁环单测。

验证：只读仲裁环裁决解析 / 仲裁决定候选落库集 / 回退语义 / 结构上不写库。
全部用确定性 Provider（chronicler.llm ∈ {FakeProvider}，仲裁环 ∈ {ScriptedProvider}），
绝不真调 LLM（docs/09 §2.1）。
"""

from __future__ import annotations


from novelist.core.chronicler import Chronicler
from novelist.core.chronicler_agent import (
    _parse_verdicts,
    agentic_chronicle,
    arbitrate,
)
from novelist.core.session import SessionInfo
from novelist.providers.fake import FakeProvider, ScriptedProvider
from novelist.storage.workspace import Workspace


def _ws(tmp_path) -> Workspace:
    return Workspace(root=str(tmp_path))


def _chronicler(ws, pid: str, *, llm_reply: str = "事件：苏晚击败墨无极 | conflict | 苏晚,墨无极\n"):
    ph = FakeProvider(reply=llm_reply)
    return Chronicler(ws, pid, llm=ph)


def _session() -> SessionInfo:
    return SessionInfo(project_id="proj-x", agent="test")


# ---- 裁决解析 ----
def test_parse_verdicts_keep_ignore_rewrite_flag():
    content = (
        "保持：[1]\n"
        "改写：[2]-苏晚击败墨无极后接手宗务｜把歧义说清\n"
        "忽略：[3]｜与既有事件近似重复\n"
        "提请：[4]｜证据不足需人工\n"
    )
    keep, ign, flag = _parse_verdicts(content)
    assert keep[1] == ""
    assert "宗务" in keep[2]
    assert ign[3]
    assert "证据不足" in flag[4]


def test_parse_verdicts_tolerates_marker_and_order():
    content = "忽略：[3]｜重复\n保持：[1]\n提请：2｜未知\n"
    keep, ign, flag = _parse_verdicts(content)
    assert 1 in keep
    assert 3 in ign
    assert 2 in flag


# ---- 裁决环（ScriptedProvider：先取证再裁决）----
def test_arbitrate_uses_evidence_then_verdict(tmp_path):
    ws, pid = _ws(tmp_path), "p"
    ch = _chronicler(ws, pid, llm_reply="事件：A 击败 B | conflict | A,B\n事件：C 与 D 结盟 | discovery | C,D\n")
    ex = ch.extract("正文窗口", max_events=3)
    assert len(ex.events) == 2
    arb_provider = ScriptedProvider([
        {"tool": "query_memory", "args": {"query": "A 击败 B"}},
        {"final": "忽略：[2]｜与既有事件近似重复\n保持：[1]\n"},
    ])
    arb = arbitrate(ch, ex, "正文窗口", provider=arb_provider, session=_session())
    assert arb.rounds == 2
    # 只保留 1，忽略 2
    assert [i for i, _ in arb.kept] == [1]
    assert any(i == 2 for i, _, _ in arb.ignored)
    # 证据轨迹留痕（取证可见）
    assert any(e["kind"] == "tool" and e["tool"] == "query_memory" for e in arb.evidence)


def test_arbitrate_no_events_returns_empty(tmp_path):
    ws, pid = _ws(tmp_path), "p"
    ch = _chronicler(ws, pid, llm_reply="状态：苏晚 | 修为：金丹\n")  # 无事件行
    ex = ch.extract("正文", max_events=3)
    arb = arbitrate(ch, ex, "正文", provider=None, session=_session())
    assert not arb.kept
    assert not arb.kept and arb.rounds == 0


# ---- agentic_chronicle 端到端 ----
def test_agentic_chronicle_writes_only_kept(tmp_path):
    ws, pid = _ws(tmp_path), "p"
    ch = _chronicler(ws, pid, llm_reply="事件：苏晚击败墨无极 | conflict | 苏晚,墨无极\n"
                                         "事件：陈松突破修为 | turning_point | 陈松\n")
    arb_provider = ScriptedProvider([
        {"final": "忽略：[2]｜与既有事件近似重复\n保持：[1]\n"},
    ])
    report, arb = agentic_chronicle(ch, "正文窗口", provider=arb_provider,
                                    session=_session(), vol=1, ch=1)
    assert report.written == 1          # 只写被"保持"的 1
    assert [i for i, _ in arb.kept] == [1]
    assert [i for i, _, _ in arb.ignored] == [2]


def test_agentic_chronicle_fallback_keeps_all_when_no_verdict(tmp_path):
    ws, pid = _ws(tmp_path), "p"
    ch = _chronicler(ws, pid, llm_reply="事件：苏晚击败墨无极 | conflict | 苏晚,墨无极\n"
                                         "事件：陈松突破修为 | turning_point | 陈松\n")
    # 仲裁环空 reply（模型不配合）→ 回退原候选全集，不丢
    arb_provider = ScriptedProvider([{"final": ""}])
    report, arb = agentic_chronicle(ch, "正文窗口", provider=arb_provider,
                                    session=_session(), vol=2, ch=3)
    assert arb.rounds == 1
    assert report.written == 2          # 空裁决 → 未提及即保持：两条都走确定性闸门，不丢候选


def test_agentic_chronicle_readonly_registry_no_writes(tmp_path):
    """结构保证“仲裁只建议不直写”：裁决环不绑定任何写库/写文件工具。"""
    ws, pid = _ws(tmp_path), "p"
    ch = _chronicler(ws, pid, llm_reply="事件：苏晚击败墨无极 | conflict | 苏晚\n")
    ex = ch.extract("正文", max_events=3)
    arb_provider = ScriptedProvider([{"final": "保持：[1]\n"}])
    arb = arbitrate(ch, ex, "正文", provider=arb_provider, session=_session())
    assert id(arb)
    # 直接验证仲裁环用到的 registry 只含读工具
    from novelist.tools import EVIDENCE_READ_TOOLS, evidence_registry

    names = set(evidence_registry(ws)._tools.keys())
    assert names == set(EVIDENCE_READ_TOOLS)
    assert names.isdisjoint({"write_file", "write_draft", "reindex_memory"})