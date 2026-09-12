"""批次三（2026-09-05 用户拍板 1/2/3/4 全做）测试。

覆盖：
- 方案1 蓝图连读：run_blueprint_review 解析+落盘 / load_blueprint_bans 过滤
- 方案2 接缝审查：_seam_retell 命中/放行/失败降级
- 方案3 卷末事实清单：build_volume_facts / load_prev_facts / compact_for_polish
- 方案4 润色全局化：global_context 进 prompt；无全局块不注入
"""

from __future__ import annotations

import json

from novelist.core.llm import LLMResult, Usage
from novelist.core.orchestrator import _seam_retell
from novelist.core.polish import build_polish_prompt
from novelist.core.volume_facts import (build_volume_facts, compact_for_polish,
                                        load_prev_facts)
from novelist.providers.fake import FakeProvider
from novelist.storage.workspace import Workspace

BP_FINDINGS = {
    "contradictions": [
        {"where": "世界观", "issue": "灵气复苏时间前后矛盾", "severity": "high"},
        {"where": "卷规划", "issue": "卷名与世界观不符", "severity": "low"},
    ],
    "character_conflicts": [
        {"who": "沈万钧", "issue": "与反派动机撞型"},
    ],
    "structure_notes": "",
    "must_watch": ["第一章必须交代镇守司的存在方式"],
}


class _ReplyProvider(FakeProvider):
    """固定 JSON 回复（模拟审查类调用的返回）。"""

    def __init__(self, payload: str) -> None:
        super().__init__(reply=payload)
        self.prompts: list[str] = []

    def complete(self, req):
        self.prompts.append(req.messages[-1].content or "")
        return LLMResult(ok=True, content=self._reply, finish_reason="stop",
                         provider="fake", usage=Usage(tokens_in=0, tokens_out=0))


def _ws(tmp_path) -> Workspace:
    return Workspace(root=str(tmp_path))


# ---------------------------------------------------------------- 方案1 蓝图连读

def test_blueprint_review_parses_and_persists(tmp_path):
    from novelist.forge.coherence import BP_FINDINGS_REL, run_blueprint_review
    from novelist.forge.state import Blueprint

    ws = _ws(tmp_path)
    pid = "p-bp"
    ws.create_project(pid)

    bp = Blueprint.blank()
    bp.data["worldview"] = {"rules": ["灵气复苏"]}
    bp.data["characters"] = [{"id": "char:1", "name": "叶蓝", "role": "protagonist"}]
    bp.data["volumes"] = [{"vol": 1, "title": "卷一"}]
    prov = _ReplyProvider("```json\n" + json.dumps(BP_FINDINGS, ensure_ascii=False) + "\n```")
    fnd = run_blueprint_review(ws, pid, bp, prov)
    assert fnd is not None
    assert len(fnd["contradictions"]) == 2
    # 落盘可读
    data = json.loads(ws._abs(f"{pid}/{BP_FINDINGS_REL}").read_text(encoding="utf-8"))
    assert data["contradictions"][0]["severity"] == "high"


def test_blueprint_bans_filters_high_and_watch(tmp_path):
    from novelist.forge.coherence import BP_FINDINGS_REL, load_blueprint_bans

    ws = _ws(tmp_path)
    pid = "p-bans"
    ws.create_project(pid)
    ws.write_json(ws._abs(f"{pid}/{BP_FINDINGS_REL}"), BP_FINDINGS)
    bans = load_blueprint_bans(ws, pid)
    assert any("灵气复苏" in b for b in bans)          # high 矛盾入选
    assert not any("卷名" in b for b in bans)          # low 矛盾不进禁令
    assert any("撞型" in b for b in bans)
    assert any("镇守司" in b for b in bans)            # must_watch 入选


def test_blueprint_bans_absent_file(tmp_path):
    from novelist.forge.coherence import load_blueprint_bans

    ws = _ws(tmp_path)
    ws.create_project("p-none")
    assert load_blueprint_bans(ws, "p-none") == []


# ---------------------------------------------------------------- 方案2 接缝审查

def test_seam_retell_hit():
    prov = _ReplyProvider('{"retell": true, "what": "母亲来电通知灵气波动"}')
    got = _seam_retell(prov, "", "他刚接过母亲的电话，得知灵气波动。",
                       "手机铃声响起，屏幕上跳动着'母亲'两个字。")
    assert got == "母亲来电通知灵气波动"


def test_seam_retell_pass():
    prov = _ReplyProvider('{"retell": false, "what": ""}')
    assert _seam_retell(prov, "", "上文结尾。", "新片段开头。") == ""


def test_seam_retell_failure_degrades_to_pass():
    class _Boom(FakeProvider):
        def complete(self, req):
            raise RuntimeError("boom")

    assert _seam_retell(_Boom(), "", "a", "b") == ""


# ---------------------------------------------------------------- 方案3 卷末事实清单

def test_volume_facts_build_and_load(tmp_path):
    ws = _ws(tmp_path)
    pid = "p-facts"
    ws.create_project(pid)
    ws.write_text(ws.draft_path(pid, 1, 1), "# 第1章\n叶蓝突破炼气三层。\n")
    ws.write_text(ws.draft_path(pid, 1, 2), "# 第2章\n叶蓝与沈万钧结盟。\n")
    facts_md = ("## 人物状态\n- 叶蓝：炼气三层\n\n## 时间线\n- 故事内时间推进：未明\n\n"
                "## 未回收伏笔\n- [第1章] 神秘玉佩\n\n## 未决冲突\n- 镇守司追查")
    prov = _ReplyProvider(facts_md)
    assert build_volume_facts(ws, pid, prov, 1) is True
    assert "通读" in prov.prompts[0] and "第 1 卷" in prov.prompts[0]
    assert "炼气三层" in prov.prompts[0]
    # 读取：vol=2 读到 vol=1 的清单；vol=1 读不到
    assert "神秘玉佩" in load_prev_facts(ws, pid, 2)
    assert load_prev_facts(ws, pid, 1) == ""


def test_volume_facts_no_chapters(tmp_path):
    ws = _ws(tmp_path)
    ws.create_project("p-empty")
    assert build_volume_facts(ws, "p-empty", _ReplyProvider("x"), 1) is False


def test_volume_facts_compact_for_polish(tmp_path):
    ws = _ws(tmp_path)
    pid = "p-comp"
    ws.create_project(pid)
    facts_md = ("# 第 1 卷末事实清单\n\n## 人物状态\n- 叶蓝：炼气三层\n\n"
                "## 时间线\n- 推进\n\n## 未回收伏笔\n- [第1章] 玉佩\n")
    ws.write_text(ws._abs(f"{pid}/workspace/forge/volume-facts-v1.md"), facts_md)
    got = compact_for_polish(ws, pid, 1)
    assert "炼气三层" in got and "玉佩" in got
    assert "时间线" not in got  # 润色只要人物状态 + 伏笔两节


# ---------------------------------------------------------------- 方案4 润色全局化

def test_polish_prompt_includes_global_context():
    text = "他站在山巅。" * 30
    gc = "【全书视野（通读顺稿用，禁止写进正文）】\n本章位置：第 1 卷第 3 章"
    p = build_polish_prompt(text, tone=None, is_chapter=True, global_context=gc)
    assert "全书视野" in p and "第 3 章" in p
    assert p.index("全书视野") < p.index("原文（")  # 视野块在原文之前


def test_polish_prompt_without_global_context():
    text = "他站在山巅。" * 30
    p = build_polish_prompt(text, tone=None, is_chapter=True)
    assert "全书视野" not in p
