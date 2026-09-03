"""D1 回归：DeepSeek json_object 400（prompt 无 "json" 字样）修复验证。

背景（2026-09-03 新书 5 章实测）：forge run_node 统一以 response_format=json_object
调 LLM，DeepSeek 要求 prompt 必须含 "json" 字样（OpenAI 无此约束）。worldview /
character_group 等节点 prompt 模板未写 "json" → 400 节点回退。修复 = run_node 入口
`_ensure_json_hint` 按需追加 JSON 引导（只影响不含 json 字样的调用）。
"""

from __future__ import annotations

from novelist.forge import Blueprint
from novelist.forge.nodes import NodeContext, _ensure_json_hint, _PROMPTS
from novelist.providers.fake import FakeProvider

SEED_META = {"title": "t", "genre": "修仙", "logline": "x",
             "scale": {"volumes": 1, "chapters_per_volume": 1, "target_words_per_chapter": 100}}


def test_ensure_json_hint_appends_when_missing():
    system, user = _ensure_json_hint("你是世界观构建师。", "细化世界观，rules 每条一句话。")
    assert "json" in (system + user).lower()
    assert user != "细化世界观，rules 每条一句话。"  # 被追加


def test_ensure_json_hint_keeps_existing_prompt_untouched():
    # 含 json 字样的调用必须原样返回（不改变既有成功路径行为）
    s2, u2 = _ensure_json_hint("输出 JSON。", "返回 JSON 对象。")
    assert (s2, u2) == ("输出 JSON。", "返回 JSON 对象。")


def test_run_node_reachable_prompts_all_carry_json_hint(ws_factory):
    """run_node 入口对全部已注册节点 prompt 做兜底：注入后必含 json 字样。

    对构造 ctx 零依赖的 prompt 全量断言；worldview/character_group 是 D1 原始故障点，
    必须覆盖（其模板本身不含 "json"，全靠注入兜底）。
    """
    ws, pid = ws_factory("proj-d1h")
    bp = Blueprint.blank(dict(SEED_META))
    ctx = NodeContext(ws=ws, project_id=pid, bp=bp, provider=FakeProvider(reply="x"),
                      pack={}, spec=None)
    checked = []
    for kind in ("worldview", "character_group", "system", "setting_entry", "style",
                 "thread_set", "book", "volume", "chapter", "arc", "beat"):
        try:
            system, user = _PROMPTS[kind](ctx)
        except Exception:
            continue  # prompt 需父节点产物字段，非 D1 覆盖面
        s2, u2 = _ensure_json_hint(system, user)
        assert "json" in (s2 + u2).lower(), f"{kind}: 注入后仍无 json 字样"
        checked.append(kind)
    assert "worldview" in checked and "character_group" in checked, "故障点节点未被覆盖"


def test_worldview_prompt_itself_lacks_json_directive(ws_factory):
    """防回归锚点：worldview 模板当前不含 json 字样（若未来模板补词，本测试提示可删）。"""
    ws, pid = ws_factory("proj-d1w")
    bp = Blueprint.blank(dict(SEED_META))
    ctx = NodeContext(ws=ws, project_id=pid, bp=bp, provider=FakeProvider(reply="x"),
                      pack={}, spec=None)
    system, user = _PROMPTS["worldview"](ctx)
    assert "json" not in (system + user).lower()
