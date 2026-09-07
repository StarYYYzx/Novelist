"""M3aa 批次：ADR-032 F2 reviewer 审校 Agent 化单测。

验证：证据环产出工单 / ok 收敛 / 取证轨迹留痕 / 失败回退单发。全部确定性 Provider，
绝不真调 LLM（docs/09 §2.1）。
"""

from __future__ import annotations

from novelist.consistency.reviewer_agent import AgenticReview, agentic_review
from novelist.core.session import SessionInfo
from novelist.providers.fake import FakeProvider, ScriptedProvider
from novelist.storage.workspace import Workspace


def _ws(tmp_path):
    ws = Workspace(root=str(tmp_path))
    # 造最小设定圣经/细纲，让 _bible_brief 与 gist 有内容可读
    (ws._abs("p/bible")).mkdir(parents=True, exist_ok=True)
    (ws._abs("p/bible/characters.json")).write_text(
        '[{"id":"c1","name":"苏晚","gender":"female","core_traits":["沉稳"]}]',
        encoding="utf-8")
    (ws._abs("p/outline")).mkdir(parents=True, exist_ok=True)
    return ws


def _session():
    return SessionInfo(project_id="p", agent="test")


# 正文缺省不给 grep/read 报错
TEXT = "苏晚一掌拍下，墨无极闷哼跪地。她转身时，袖中那枚早已毁去的玉佩却完好无损。"


def test_agentic_review_produces_verified_issues(tmp_path):
    ws = _ws(tmp_path)
    prov = ScriptedProvider([
        {"tool": "query_memory", "args": {"query": "玉佩 毁去"}},
        {"final": "warn | 事实前后矛盾 | 玉佩在记忆里已毁去，本章却完好 | 核对玉佩去向\n"},
    ])
    ar = agentic_review(TEXT, 1, 1, ws=ws, project_id="p", provider=prov,
                        session=_session(), max_rounds=4)
    assert ar.mode == "agent"
    assert ar.rounds == 2
    assert len(ar.issues) == 1
    assert ar.issues[0].category == "事实前后矛盾"
    assert ar.issues[0].level == "warn"
    assert "玉佩" in ar.issues[0].detail
    # 取证留痕
    assert any(e["kind"] == "tool" and e["tool"] == "query_memory" for e in ar.evidence)


def test_agentic_review_ok_means_no_issues(tmp_path):
    ws = _ws(tmp_path)
    prov = ScriptedProvider([{"final": "ok"}])
    ar = agentic_review(TEXT, 1, 1, ws=ws, project_id="p", provider=prov,
                        session=_session(), max_rounds=2)
    assert ar.mode == "agent"
    assert ar.issues == []


def test_agentic_review_empty_text_no_llm(tmp_path):
    ws = _ws(tmp_path)
    ar = agentic_review("", 1, 1, ws=ws, project_id="p", provider=None,
                        session=_session())
    assert ar.issues == [] and ar.mode == "agent"


def test_agentic_review_fallback_on_error(tmp_path):
    """证据环抛错/流程异常 → 回退单发 Reviewer.review，绝不出空工单。"""
    ws = _ws(tmp_path)
    prov = FakeProvider(reply="block | 人设漂移 | 苏晚言行不符人物卡 | 收敛语气")

    class BoomProvider(FakeProvider):
        _first = True

        def complete(self, req):
            if self._first:
                self._first = False
                raise RuntimeError("boom")
            return super().complete(req)

    bp = BoomProvider(reply="block | 人设漂移 | 苏晚言行不符人物卡 | 收敛语气")
    ar = agentic_review(TEXT, 1, 1, ws=ws, project_id="p", provider=bp,
                        session=_session(), max_rounds=2)
    assert ar.mode == "fallback"
    assert any(i.category == "人设漂移" for i in ar.issues)