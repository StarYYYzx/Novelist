"""D1 细纲情节工艺（tension/hook）测试（2026-09-04 拍板）。

- prompt 强制输出 tension/hook 字段；
- gist 落盘透传（模型没给留空，绝不 block——向后兼容旧细纲）；
- render_gist_md 行内注入（整篇 md 进章级 goal → 每个事件 prompt 可见），
  张力行带"每个事件都要服务于它"、钩子行带"最后一个事件必须以此收尾"的纪律语。
"""

from __future__ import annotations


from novelist.forge import Blueprint
from novelist.forge.nodes import NodeContext, _chapter_prompt, render_gist_md
from novelist.providers.fake import FakeProvider


def test_render_gist_md_inlines_tension_hook():
    gist = {"title": "夜袭", "key_events": ["暗影夜袭", "苏老赶到"],
            "turns": ["婉儿身份初露"], "tension": "保人还是藏拙，两难",
            "hook": "苏老多看了两秒", "characters": ["char:p"],
            "threads_involved": [], "after_days": 0}
    md = render_gist_md(gist, 1, 3, ["李天劫"])
    assert "- 全章张力（每个事件都要服务于它，不得偏离）：保人还是藏拙，两难" in md
    # A3 修正（2026-09-05）：钩子行降级为收束方向指引——防前置事件抢跑写钩子
    assert "章末钩子（收束方向指引" in md and "严禁提前写钩子内容" in md
    assert "仅最后一个事件以此收尾" in md


def test_render_gist_md_without_tension_hook_backcompat():
    """旧 gist（无 tension/hook 键）渲染不炸、不注入空行。"""
    gist = {"title": "夜袭", "key_events": ["暗影夜袭"], "turns": [],
            "characters": ["char:p"], "threads_involved": ["pt:1"], "after_days": 1}
    md = render_gist_md(gist, 1, 3, ["李天劫"])
    assert "全章张力" not in md and "章末钩子" not in md
    assert "- 伏笔：pt:1" in md  # 原有行不受影响


def test_chapter_prompt_requires_tension_hook(ws_factory):
    """细纲师 prompt 的输出 JSON 契约含 tension/hook 字段。"""
    ws, pid = ws_factory("proj-th")
    bp = Blueprint.blank({"title": "t", "genre": "修仙", "logline": "x",
                          "scale": {"volumes": 1, "chapters_per_volume": 3,
                                    "target_words_per_chapter": 100}})
    ctx = NodeContext(ws=ws, project_id=pid, bp=bp, provider=FakeProvider(reply="x"),
                      pack={}, vol=1, ch=2)
    _sys, user = _chapter_prompt(ctx)
    assert '"tension"' in user and '"hook"' in user
    assert "张力" in user and "钩子" in user
