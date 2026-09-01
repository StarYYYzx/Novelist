"""M0 里程碑单元测试（docs/08 M0）。

覆盖：工作区创建/沙箱/原子写、检查点快照/校验/恢复、schema 校验与实体模型、
OpenAI 兼容适配器（无 key 报错 + httpx mock 解析）、CLI init。
"""

from __future__ import annotations

import json

import pytest

from novelist.storage.checkpoint import Checkpoint, CheckpointError
from novelist.storage.models import SchemaRegistry, SchemaError
from novelist.storage.workspace import Workspace, WorkspaceError


# ---------- workspace ----------


def test_workspace_create_project_skeleton(tmp_path):
    ws = Workspace(root=str(tmp_path))
    pid = "proj-demo"
    root = ws.create_project(pid)
    assert (root / "bible").is_dir()
    assert (root / "outline" / "chapters").is_dir()
    assert (root / "memory" / "character_histories").is_dir()
    assert (root / "drafts" / "chapters").is_dir()
    assert ws.exists(pid)


def test_workspace_rejects_path_traversal(tmp_path):
    ws = Workspace(root=str(tmp_path))
    with pytest.raises(WorkspaceError):
        ws._abs("../escape")


def test_workspace_atomic_write_json(tmp_path):
    ws = Workspace(root=str(tmp_path))
    pid = "proj-w"
    ws.create_project(pid)
    p = ws.bible_path(pid, "characters")
    ws.write_json(p, {"name": "苏晚"})
    assert json.loads(p.read_text(encoding="utf-8")) == {"name": "苏晚"}
    # 不应残留临时文件
    assert not p.with_suffix(".tmp").exists()


def test_workspace_invalid_project_id(tmp_path):
    ws = Workspace(root=str(tmp_path))
    with pytest.raises(WorkspaceError):
        ws.project_dir("bad id/..")


# ---------- checkpoint ----------


def test_checkpoint_save_verify_roundtrip(tmp_path):
    ws = Workspace(root=str(tmp_path))
    pid = "proj-ck"
    ws.create_project(pid)
    ck = Checkpoint(ws)
    project = {"id": pid, "pipeline_state": "立项", "prefs": {}}
    ck.save(pid, project)
    assert ck.verify(pid) == []
    restored = ck.restore(pid)
    assert restored["id"] == pid


def test_checkpoint_detects_tampering(tmp_path):
    ws = Workspace(root=str(tmp_path))
    pid = "proj-t"
    ws.create_project(pid)
    ck = Checkpoint(ws)
    ck.save(pid, {"id": pid, "pipeline_state": "大纲"})
    # 篡改 project.json（已纳入 checksum）-> 校验应发现不一致
    pj = ws.project_json_path(pid)
    pj.write_text('{"id":"proj-t","pipeline_state":"篡改"}', encoding="utf-8")
    assert ck.verify(pid) != []
    with pytest.raises(CheckpointError):
        ck.restore(pid)


# ---------- models / schema ----------


def test_schema_registry_validates_valid_bible(tmp_path):
    reg = SchemaRegistry(root=str(tmp_path))
    # 从真实 schemas 目录加载 characters schema 校验一段合法人物卡
    from pathlib import Path as P

    schemas_root = P("schemas").resolve()
    reg = SchemaRegistry(root=schemas_root)
    # F0'：characters schema 根已修正为数组（磁盘实然，docs/06 §3.1）
    data = [{"id": "char:cz7", "name": "苏晚", "status": "active"}]
    reg.validate("bible/characters", data)  # 不抛即通过


def test_schema_registry_rejects_bad_character(tmp_path):
    from pathlib import Path as P

    schemas_root = P("schemas").resolve()
    reg = SchemaRegistry(root=schemas_root)
    with pytest.raises(Exception):
        reg.validate("bible/characters", [{"id": "bad", "name": ""}])  # name 空 -> 校验失败


def test_models_character_pydantic():
    from novelist.storage.models import Character

    c = Character(id="char:x", name="阿晚")
    assert c.status == "active"
    assert c.core_traits == []


# ---------- openai compatible provider ----------


def test_openai_provider_requires_key():
    from novelist.core.errors import ProviderError
    from novelist.providers.openai import OpenAICompatibleProvider

    p = OpenAICompatibleProvider(api_key="", base_url="https://example.com/v1")
    with pytest.raises(ProviderError):
        p.complete(req=_req())


def _req():
    from novelist.core.llm import LLMMessage, LLMRequest

    return LLMRequest(messages=[LLMMessage(role="user", content="hi")])


def test_openai_parse_completion_ok():
    from novelist.providers.openai import parse_completion

    res = parse_completion(
        200,
        {
            "choices": [{"message": {"content": "你好"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        },
    )
    assert res.ok
    assert res.content == "你好"
    assert res.usage is not None and res.usage.tokens_in == 5


def test_openai_parse_completion_moderation_block():
    from novelist.providers.openai import parse_completion

    res = parse_completion(
        200,
        {"choices": [{"message": {"refusal": "unsafe"}, "finish_reason": "content_filter"}]},
    )
    assert res.ok is False
    assert res.blocked is True
    assert res.block_reason == "content_filter"


def test_openai_parse_completion_http_451_block():
    from novelist.providers.openai import parse_completion

    res = parse_completion(451, {})
    assert res.ok is False
    assert res.blocked is True
    assert res.block_reason == "http_451"


def test_openai_parse_tool_calls():
    from novelist.providers.openai import parse_completion

    res = parse_completion(
        200,
        {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "c1",
                                "function": {"name": "query_memory", "arguments": '{"query":"近况"}'},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
    )
    assert res.ok
    assert len(res.tool_calls) == 1
    assert res.tool_calls[0].name == "query_memory"
    assert res.tool_calls[0].arguments == {"query": "近况"}


# ---------- CLI ----------


def test_cli_init_creates_project(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from novelist.cli import cli

    runner = CliRunner()
    res = runner.invoke(cli, ["--config", "NOPE", "init", str(tmp_path)], obj={"config": None})
    # 无配置文件应能用默认配置 + 传入目录创建
    res = runner.invoke(
        cli,
        ["init", str(tmp_path)],
    )
    assert res.exit_code == 0
    # project.json 已创建
    import pathlib

    proj_dirs = [d for d in pathlib.Path(tmp_path).iterdir() if d.is_dir()]
    assert any((d / "project.json").exists() for d in proj_dirs)

