"""F0'（schema 对齐）测试：bible 契约校验层（core/bible.py）与 schemas/ 契约。

覆盖（docs/11 编码规范）：
- 契约映射完整性：BIBLE_CONTRACT 每个 schema 文件存在（先契约后实现，docs/07）。
- 样例数据全 PASS：数组根 / 扁平 style / worldview id+扩展字段 / characters 扩展字段 /
  timeline oneOf 新旧格式 / plot_event 六类 enum / fragment_index object 根 /
  character_history state_delta null / project id 无冒号。
- validate_project 行为：全过返回 []；违规文件记一条；缺失文件不违规。
- parse_gist：旧行内格式 / front-matter 块 / 缺失返回 None。
- 集成（skipif）：对 novel_workspace/proj-t5 全过（磁盘实然满足契约）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import jsonschema

from novelist.core.bible import BIBLE_CONTRACT, parse_gist, validate_project
from novelist.storage.models import SchemaError, SchemaRegistry

SCHEMAS_ROOT = Path(__file__).resolve().parents[1] / "schemas"


# ---- 契约映射完整性 ----

def test_contract_every_schema_exists():
    for rel, schema_name in BIBLE_CONTRACT:
        p = SCHEMAS_ROOT / f"{schema_name}.schema.json"
        assert p.exists(), f"BIBLE_CONTRACT 引用缺失 schema: {schema_name} ({rel})"


def test_contract_covers_key_factsources():
    names = [n for _, n in BIBLE_CONTRACT]
    for key in ["bible/worldview", "bible/characters", "bible/style", "bible/worldstate",
                "outline/volume", "memory/plot_event", "memory/fragment_index", "project"]:
        assert key in names, f"契约映射缺 {key}"


# ---- 样例数据校验全 PASS ----

def _validate(schema_name: str, data) -> None:
    SchemaRegistry(root=SCHEMAS_ROOT).validate(schema_name, data)


@pytest.mark.parametrize("schema_name,data", [
    # 数组根 + 磁盘实然字段（含扩展字段 possessions）
    ("bible/characters", [
        {"id": "char:luchen", "name": "陆沉", "gender": "male", "is_protagonist": True,
         "core_traits": ["谨慎隐忍"], "background": "落霞宗杂役弟子",
         "first_appear": {"vol": 1, "ch": 1}, "possessions": ["云纹玉牌"]},
    ]),
    # locations 数组根 + aliases
    ("bible/locations", [
        {"id": "loc:zayiyuan", "name": "杂役院", "aliases": ["杂役院柴房"]},
    ]),
    # items 数组根 + type + desc/note 并存
    ("bible/items", [
        {"id": "item:yupai", "name": "云纹玉牌", "type": "artifact",
         "aliases": ["玉牌"], "desc": "母亲遗物", "note": "宗主信物"},
    ]),
    # settings 数组根
    ("bible/settings", [
        {"id": "set:xisuidan", "keywords": ["洗髓丹"], "text": "练气辅助丹药",
         "revealed": True, "first_ch": 1},
    ]),
    # plot_threads 数组根 + active/paid_off 流转
    ("bible/plot_threads", [
        {"id": "thread:yunwenling", "status": "paid_off", "desc": "云纹玉牌来历",
         "scope": "volume", "target_vol": 1, "planted": {"vol": 1, "ch": 1},
         "returned": {"vol": 1, "ch": 4}},
        {"id": "pt:guixuhui", "status": "active", "desc": "归墟会阴谋", "scope": "book",
         "planted": {"vol": 1, "ch": 3}},
    ]),
    # timeline 数组根：空数组（新项目）合法
    ("bible/timeline", []),
    # timeline 新格式（相对天数轴，ADR-019）
    ("bible/timeline", [
        {"id": "tl:13", "event": "叶蓝服丹闭关", "at": {"t": 1143, "vol": 1, "ch": 12},
         "in_chapters": [{"vol": 1, "ch": 12}]},
    ]),
    # timeline 旧格式（历法式，oneOf 兼容分支）
    ("bible/timeline", [
        {"id": "tl:1", "event": "苏晚被逐出内门",
         "at": {"era": "青冥历", "year": 1137, "season": "春"},
         "in_chapters": [{"vol": 1, "ch": 1}]},
    ]),
    # worldstate（M3m：time/pending）
    ("bible/worldstate", {
        "time": {"now": 1143, "origin_text": "叶蓝穿越之日"},
        "pending": [{"id": "pd:1", "who": "char:yelan", "what": "叶蓝出关",
                     "due": 1233, "span": 90, "created_t": 1143,
                     "status": "scheduled", "created_at": {"vol": 1, "ch": 12}}],
        "characters": {},
    }),
    # style 扁平重构（pov/tone 数组/target_words/forbidden_words/protagonist）
    ("bible/style", {
        "pov": "第三人称限知（陆沉视角）",
        "tone": ["热血激昂"],
        "target_words_per_chapter": 2400,
        "forbidden_words": ["打卡", "手机"],
        "protagonist": {"id": "char:luchen", "name": "陆沉", "gender": "male"},
    }),
    # worldview：id + 代码实然扩展字段
    ("bible/worldview", {
        "id": "world:luoxia", "name": "落霞界",
        "power_system": {"levels": ["练气", "筑基"]},
        "phase_policy": {"opening_chapters": 2, "tail_chapters": 2},
        "unavailable_states": ["闭关", "渡劫"],
        "factions": [{"faction": "归墟会", "note": "邪修组织"}],
        "rules": ["云纹令只认陆氏血脉"],
    }),
    # outline/volume 数组根
    ("outline/volume", [
        {"id": "vol:1", "vol": 1, "title": "云纹令", "summary": "身世真相",
         "chapter_range": [1, 5], "target_words": 12000,
         "threads_to_payoff": ["thread:yunwenling"]},
    ]),
    # plot_event 数组根 + 六类 enum + thread 前缀 + ev id 含冒号
    ("memory/plot_event", [
        {"id": "ev:proj-t5:1:1:e11", "at": {"vol": 1, "ch": 1}, "type": "discovery",
         "summary": "玉牌发烫", "participants": ["char:luchen"],
         "affected_threads": ["thread:yunwenling"]},
        {"id": "ev:proj-t5:1:2:e12", "at": {"vol": 1, "ch": 2}, "type": "dialogue",
         "summary": "云曦赠丹", "participants": ["char:yunxi"]},
        {"id": "ev:proj-t5:1:5:e15", "at": {"vol": 1, "ch": 5}, "type": "chapter",
         "summary": "第一卷收尾"},
    ]),
    # fragment_index object 根 + text/char_id
    ("memory/fragment_index", {
        "revision": 68, "kind": "keyword", "dim": None,
        "fragments": [
            {"sig": "plot_event:1.1:25a95234e1", "kind": "plot_event",
             "text": "云曦赠予陆沉洗髓丹", "source": {"vol": 1, "ch": 1},
             "refs": ["char:luchen"], "hash": "abc", "char_id": None},
        ],
    }),
    # character_history state_delta null 合法
    ("memory/character_history", {
        "char_id": "char:hepao", "revision": 3,
        "entries": [
            {"at": {"vol": 1, "ch": 2}, "summary": "黑袍人突袭云曦", "state_delta": None},
        ],
    }),
    # project id 无冒号 + title
    ("project", {"id": "proj-t5", "title": "云纹令", "pipeline_state": "正文"}),
])
def test_sample_data_passes_contract(schema_name, data):
    _validate(schema_name, data)


def test_worldview_requires_id():
    with pytest.raises(SchemaError):
        _validate("bible/worldview", {"name": "落霞界"})


def test_characters_requires_id_name():
    with pytest.raises(SchemaError):
        _validate("bible/characters", [{"name": "陆沉"}])


# ---- validate_project 行为 ----

def test_validate_project_all_pass(ws_factory, write_json):
    ws, pid = ws_factory()
    write_json(ws, pid, "bible/worldview.json", {"id": "world:t", "name": "测试界"})
    write_json(ws, pid, "bible/characters.json", [{"id": "char:a", "name": "甲"}])
    write_json(ws, pid, "outline/volumes.json", [])
    violations = validate_project(ws, pid)
    assert violations == []


def test_validate_project_missing_files_not_violation(ws_factory):
    ws, pid = ws_factory()
    # 只写 project.json 骨架（ws_factory 已建），其余全部缺失 → 不算违规
    assert validate_project(ws, pid) == []


def test_validate_project_reports_violation(ws_factory, write_json):
    ws, pid = ws_factory()
    write_json(ws, pid, "bible/worldview.json", {"name": "缺 id 的世界观"})
    write_json(ws, pid, "bible/characters.json", [{"id": "char:a", "name": "甲"}])
    violations = validate_project(ws, pid)
    assert len(violations) == 1
    v = violations[0]
    assert v.schema == "bible/worldview"
    assert Path(v.path).name == "worldview.json"
    assert any("id" in e for e in v.errors)


def test_validate_project_bad_json_reports(ws_factory):
    ws, pid = ws_factory()
    p = ws._abs(f"{pid}/bible/style.json")  # noqa: SLF001
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ 损坏的 json", encoding="utf-8")
    violations = validate_project(ws, pid)
    assert len(violations) == 1
    assert "读取" in violations[0].errors[0] or "解析" in violations[0].errors[0]


def test_validate_project_glob_histories(ws_factory, write_json):
    ws, pid = ws_factory()
    for cid in ("char:a", "char:b"):
        write_json(ws, pid, f"memory/character_histories/{cid.replace(':', '_')}.json",
                   {"char_id": cid, "revision": 1, "entries": []})
    assert validate_project(ws, pid) == []
    write_json(ws, pid, "memory/character_histories/char_c.json", {"char_id": "x"})
    violations = validate_project(ws, pid)
    assert len(violations) == 1
    assert violations[0].schema == "memory/character_history"


# ---- parse_gist（细纲 front-matter 宽松解析）----

def _gist_text_legacy() -> str:
    return (
        "# 第 1 章 玉牌发烫\n\n"
        "key_events: [杂役弟子陆沉擦洗玉牌, 云曦巡视杂役院, 云曦留下一枚洗髓丹]\n\n"
        "细纲要点：\n- 陆沉是杂役弟子。\n"
    )


def _gist_text_frontmatter() -> str:
    return (
        "---\n"
        "{\"id\": \"ch:1:1\", \"vol\": 1, \"ch\": 1, \"title\": \"玉牌发烫\",\n"
        " \"key_events\": [\"擦洗玉牌\"], \"turns\": [\"opening-hook\"]}\n"
        "---\n"
        "# 第 1 章 玉牌发烫\n\n细纲要点……\n"
    )


def test_parse_gist_legacy_inline(ws_factory):
    ws, pid = ws_factory()
    p = ws._abs(f"{pid}/outline/chapters/1-1.md")  # noqa: SLF001
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_gist_text_legacy(), encoding="utf-8")
    g = parse_gist(ws, pid, 1, 1)
    assert g is not None
    assert g["id"] == "ch:1:1"
    assert g["vol"] == 1 and g["ch"] == 1
    assert g["title"] == "玉牌发烫"
    assert g["key_events"] == ["杂役弟子陆沉擦洗玉牌", "云曦巡视杂役院", "云曦留下一枚洗髓丹"]
    # 契约形状可过 chapter_gist schema（turns 兜底空）
    _validate("outline/chapter_gist", g)


def test_parse_gist_frontmatter(ws_factory):
    ws, pid = ws_factory()
    p = ws._abs(f"{pid}/outline/chapters/1-1.md")  # noqa: SLF001
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_gist_text_frontmatter(), encoding="utf-8")
    g = parse_gist(ws, pid, 1, 1)
    assert g is not None
    assert g["title"] == "玉牌发烫"
    assert g["key_events"] == ["擦洗玉牌"]
    assert g["turns"] == ["opening-hook"]


def test_parse_gist_missing_returns_none(ws_factory):
    ws, pid = ws_factory()
    assert parse_gist(ws, pid, 9, 9) is None


def test_parse_gist_no_title_no_events(ws_factory):
    ws, pid = ws_factory()
    p = ws._abs(f"{pid}/outline/chapters/1-2.md")  # noqa: SLF001
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("只有正文\n", encoding="utf-8")
    g = parse_gist(ws, pid, 1, 2)
    assert g["title"] == "第 2 章"
    assert "key_events" not in g
    assert g["turns"] == []


# ---- 集成（磁盘实然满足契约；novel_workspace 不入 git，存在才跑）----

PROJ_T5 = Path(__file__).resolve().parents[1] / "novel_workspace" / "proj-t5"


@pytest.mark.skipif(not PROJ_T5.exists(), reason="novel_workspace/proj-t5 未检出（gitignore）")
def test_proj_t5_contract_all_pass():
    from novelist.storage.workspace import Workspace

    ws = Workspace(root=str(PROJ_T5.parent))
    violations = validate_project(ws, "proj-t5")
    assert violations == []


@pytest.mark.skipif(not PROJ_T5.exists(), reason="novel_workspace/proj-t5 未检出（gitignore）")
def test_proj_t5_gist_parses():
    from novelist.storage.workspace import Workspace

    ws = Workspace(root=str(PROJ_T5.parent))
    g = parse_gist(ws, "proj-t5", 1, 1)
    assert g is not None
    assert g["title"] == "玉牌发烫"
    assert len(g.get("key_events", [])) == 3
