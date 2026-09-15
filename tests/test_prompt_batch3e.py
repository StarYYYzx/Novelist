"""3-E 拍板批次（2026-09-15）的守卫测试。

拍板结果：S-1 **不做**（润色语言风格主观、非当前主要矛盾）；A2/S-2、A3/S-3、B/U7 做。

- S-2：导演层注入【世界观基座】（境界体系/铁律），数据源与细纲层同一份
  （bible/worldview.json → `context.worldview_base_lines`）；
- S-3：forge 卷纲/细纲/人物卡节点统一注入世界观块（含术语表/禁用词，缺省省略），
  空世界观/空文风时纯增量（无空标题）；
- U7：`[generation]` 配置段解析 + 三级优先级（显式 flag > 配置 > 出厂默认），
  无配置时行为与历史版本一致；未知键报错（拼错键静默失效比报错更糟）。
"""

from __future__ import annotations

import pytest

from novelist.config import GENERATION_KEYS, GenerationConfig, load_config, resolve_opt
from novelist.core.director import DIRECTOR_PROMPT, build_direction, load_characters
from novelist.forge.nodes import NodeContext, _chapter_prompt, _character_prompt, _volume_prompt
from novelist.forge.state import Blueprint

_WV = {"name": "青冥界", "power_system": {"levels": ["炼气", "筑基", "金丹"]},
       "rules": ["修士不可对凡人出手"]}
_STYLE = {"pov": "第三人称限知",
          "glossary": [{"term": "五五开", "note": "主角金手指"}],
          "forbidden_words": ["毫无疑问"]}


# ---------------------------------------------------------------- S-2 导演层

def test_director_prompt_has_worldview_placeholder():
    assert "{worldview_block}" in DIRECTOR_PROMPT


def test_build_direction_injects_worldview(ws_factory, stub_llm, write_json):
    ws, pid = ws_factory("proj-s2-wv")
    write_json(ws, pid, "bible/worldview.json", _WV)
    write_json(ws, pid, "bible/characters.json", [
        {"id": "char:y", "name": "叶岚", "gender": "male", "power": {"level": "炼气三层"}},
    ])
    provider = stub_llm("叶岚 | 冷静 | 话少 | 不得自称小姐")
    sheet = build_direction(ws, pid, provider, vol=1, ch=1, event_index=1,
                            ev_text="叶岚下山", cards=load_characters(ws, pid))
    assert sheet is not None
    prompt = provider.calls[-1]
    assert "【世界观基座】" in prompt
    assert "炼气、筑基、金丹" in prompt
    assert "修士不可对凡人出手" in prompt
    assert "不得违背铁律" in prompt


def test_build_direction_without_worldview_stays_clean(ws_factory, stub_llm):
    """空世界观时不出现空标题（纯增量）。"""
    ws, pid = ws_factory("proj-s2-nowv")
    provider = stub_llm("叶岚 | 冷静 | 话少 | 不得自称小姐")
    sheet = build_direction(ws, pid, provider, vol=1, ch=1, event_index=1,
                            ev_text="叶岚下山", cards=[{"id": "c1", "name": "叶岚"}])
    assert sheet is not None
    assert "【世界观基座】" not in provider.calls[-1]


# ---------------------------------------------------------------- S-3 forge 节点


def _mk_bp(ws, pid, *, wv=True, style=True):
    bp = Blueprint.blank({"title": "T"})
    bp.set("meta.scale", {"volumes": 1, "chapters_per_volume": 4})
    if wv:
        bp.set("worldview", dict(_WV))
    if style:
        bp.set("style", dict(_STYLE))
    return bp


def _mk_ctx(ws, pid, bp, vol=1, ch=1, child=None):
    return NodeContext(ws=ws, project_id=pid, bp=bp, provider=None, pack={},
                       vol=vol, ch=ch, child=child)


def test_s3_volume_and_character_prompts_inject_worldview(ws_factory):
    ws, pid = ws_factory("proj-s3-inject")
    bp = _mk_bp(ws, pid)
    _, user = _volume_prompt(_mk_ctx(ws, pid, bp, vol=1, ch=0))
    assert "【世界观基座】" in user
    assert "炼气、筑基、金丹" in user and "修士不可对凡人出手" in user
    assert "术语表" in user and "五五开" in user
    assert "禁用词" in user and "毫无疑问" in user

    _, kuser = _character_prompt(_mk_ctx(ws, pid, bp, child={"id": "char:x", "name": "叶岚"}))
    assert "【世界观基座】" in kuser
    assert "power.level 必须出自境界体系表" in kuser
    assert "五五开" in kuser


def test_s3_chapter_prompt_keeps_batch2_semantics_and_adds_terms(ws_factory):
    """细纲层：批次 2 的境界/铁律语义不变，S-3 补术语表/禁用词。"""
    ws, pid = ws_factory("proj-s3-chapter")
    bp = _mk_bp(ws, pid)
    _, user = _chapter_prompt(_mk_ctx(ws, pid, bp))
    assert "【世界观基座】" in user
    assert "炼气、筑基、金丹" in user and "修士不可对凡人出手" in user
    assert "术语表" in user and "禁用词" in user


def test_s3_empty_worldview_and_style_produce_no_block(ws_factory):
    """全空时不得多出空标题——注入是纯增量。"""
    ws, pid = ws_factory("proj-s3-empty")
    bp = _mk_bp(ws, pid, wv=False, style=False)
    for prompt in (_volume_prompt(_mk_ctx(ws, pid, bp, vol=1, ch=0))[1],
                   _chapter_prompt(_mk_ctx(ws, pid, bp))[1],
                   _character_prompt(_mk_ctx(ws, pid, bp, child={"id": "char:x"}))[1]):
        assert "【世界观基座】" not in prompt


def test_s3_glossary_absent_when_style_missing(ws_factory):
    """文风节点未跑（无 glossary/禁用词）时只注境界/铁律，不出空术语行。"""
    ws, pid = ws_factory("proj-s3-nostyle")
    bp = _mk_bp(ws, pid, style=False)
    _, user = _volume_prompt(_mk_ctx(ws, pid, bp, vol=1, ch=0))
    assert "【世界观基座】" in user
    assert "术语表（写法以此为准" not in user
    assert "禁用词（任何产出不得使用）" not in user


# ---------------------------------------------------------------- U7 配置化


def test_resolve_opt_precedence():
    assert resolve_opt(True, False, False) is True        # 显式 > 配置
    assert resolve_opt(None, False, True) is False        # 配置 > 出厂默认
    assert resolve_opt(None, None, True) is True          # 出厂默认
    assert resolve_opt(None, None, None) is None


def test_generation_config_defaults_all_none():
    cfg = GenerationConfig()
    assert all(getattr(cfg, k) is None for k in GENERATION_KEYS)


def test_load_config_parses_generation_section(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        "[generation]\n"
        "event_loop = true\n"
        "polish = true\n"
        "inject_bible = false\n"
        "gen_tokens = 6000\n"
        "min_event_words = 80\n",
        encoding="utf-8",
    )
    g = load_config(str(p)).generation
    assert g.event_loop is True and g.polish is True
    assert g.inject_bible is False
    assert g.gen_tokens == 6000 and g.min_event_words == 80
    assert g.seam_review is None  # 未配置 = 交给出厂默认


def test_load_config_rejects_unknown_generation_key(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[generation]\npolsh = true\n", encoding="utf-8")  # 拼错 polish
    from novelist.core.errors import NovelistError

    with pytest.raises(NovelistError, match="polsh"):
        load_config(str(p))


def test_load_config_without_file_has_empty_generation():
    g = load_config(None).generation
    assert isinstance(g, GenerationConfig)
    assert g.polish is None and g.gen_tokens is None


def _write_cfg(tmp_path, body: str):
    p = tmp_path / "gen.toml"
    p.write_text(body, encoding="utf-8")
    return str(p)


def test_cli_chapter_config_defaults_apply(tmp_path):
    """配置 event_loop/polish=true 且不传 flag → 生效开关回显两者（U1 行与 U7 联动）。"""
    from click.testing import CliRunner

    from novelist.cli import cli
    from novelist.storage.checkpoint import Checkpoint
    from novelist.storage.workspace import Workspace

    ws_root = tmp_path / "ws"
    ws = Workspace(root=str(ws_root))
    ws.create_project("proj-u7")
    Checkpoint(ws).save("proj-u7", {"id": "proj-u7", "title": "T",
                                    "pipeline_state": "立项", "event_seq": 0})
    cfg = _write_cfg(tmp_path, "[generation]\nevent_loop = true\npolish = true\n")
    runner = CliRunner()
    res = runner.invoke(cli, ["--config", cfg, "chapter", str(ws_root),
                              "--vol", "1", "--ch", "1", "--provider", "demo"])
    assert res.exit_code == 0, res.output
    assert "生效开关" in res.output
    assert "事件循环" in res.output and "润色" in res.output
    # 显式 --no-polish 压过配置
    res2 = runner.invoke(cli, ["--config", cfg, "chapter", str(ws_root),
                               "--vol", "1", "--ch", "2", "--provider", "demo",
                               "--no-polish"])
    assert res2.exit_code == 0, res2.output
    assert "润色" not in res2.output.split("生效开关：", 1)[1].splitlines()[0]
