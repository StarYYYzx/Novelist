"""M3z 批次 A 测试（docs/03 ADR-029，人工反馈通道）。

覆盖（**单测绝不真调 LLM**，docs/09 §2.1——全部用 ScriptedProvider）：
- 可改性白名单：合法字段可改；只读字段(id/status/主角约束/世界铁律)拒绝
- 列表条目按 id/name 定位；对象文件按点路径定位；add 铸造 id；delete 移除
- 嵌套字段路径（power.level）+ 值类型校验
- 敏感判定：世界铁律 / 已提交线索 / delete / 时间轴 → sensitive
- FeedbackParser：LLM 意见 → op 集；非白名单文件丢弃；写作类空意见
- FeedbackStore 持久化 + 状态流转（pending/applied/rejected）
- 原子写回（apply_op / apply_feedback 幂等）+ provenance/甲审留痕
- CLI feedback：parse/list/apply/deny 接线 + ApprovalQueue 集成
"""

from __future__ import annotations

import json
import re

import pytest

from novelist.core.approval import ApprovalQueue
from novelist.core.bible_feedback import (
    BIBLE_EDITABLE,
    EditOp,
    FeedbackError,
    FeedbackParser,
    apply_feedback,
    apply_op,
    feedback_persist_dir,
    flag_sensitive,
    load_store,
    validate_op,
)
from novelist.providers.fake import ScriptedProvider
from novelist.storage.workspace import Workspace


def _ws(tmp_path) -> tuple[Workspace, str]:
    ws = Workspace(root=str(tmp_path))
    pid = "proj-t"
    ws.create_project(pid)
    _put(ws, pid, "project.json", {"id": pid, "title": "t", "pipeline_state": "世界观"})
    _put(ws, pid, "bible/characters.json",
         {"characters": [{"id": "c1", "name": "苏晚", "age": 18,
                          "personality": "冷", "power": {"level": "练气"}}]})
    _put(ws, pid, "bible/worldview.json",
         {"name": "沧海", "rules": ["灵气不可逆"], "power_system": {"levels": ["练气", "筑基"]}})
    _put(ws, pid, "bible/style.json",
         {"tone": ["热血"], "protagonist": {"id": "c1"},
          "target_words_per_chapter": 2000})
    _put(ws, pid, "bible/plot_threads.json",
         {"plot_threads": [{"id": "t1", "name": "复仇线", "status": "active"}]})
    return ws, pid


def _put(ws, pid, rel, obj):
    import pathlib

    p = pathlib.Path(str(ws._abs(pid))) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _read(ws, pid, rel):
    import pathlib

    p = pathlib.Path(str(ws._abs(pid))) / rel
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 白名单与校验
# ---------------------------------------------------------------------------
def test_editable_schema_has_all_bible_files(tmp_path):
    ws, pid = _ws(tmp_path)
    for f in ("characters.json", "worldview.json", "style.json", "locations.json",
              "plot_threads.json", "items.json", "skills.json", "settings.json",
              "worldstate.json"):
        assert f in BIBLE_EDITABLE, f
    # 关键只读保护区（结构性身份/派生字段 + 实然状态）
    assert "id" in BIBLE_EDITABLE["characters.json"]["readonly"]
    assert "is_protagonist" in BIBLE_EDITABLE["characters.json"]["readonly"]
    assert "protagonist" in BIBLE_EDITABLE["style.json"]["readonly"]
    # 世界铁律是 covenant：不在只读，而是可改+标敏感
    assert "rules" in BIBLE_EDITABLE["worldview.json"]["edit"]


def test_validate_ok_and_rejects_readonly(tmp_path):
    ws, pid = _ws(tmp_path)
    validate_op(EditOp(file="characters.json", op="edit", target="苏晚",
                       field="personality", value="温"))
    validate_op(EditOp(file="characters.json", op="edit", target="c1",
                       field="power.level", value="筑基"))  # 嵌套可改
    # 世界铁律可改（sensitive），不抛错
    validate_op(EditOp(file="worldview.json", op="edit", target="rules", value=["新铁律"]))
    for bad in (
        EditOp(file="characters.json", op="edit", target="c1", field="id", value="x"),
        EditOp(file="characters.json", op="edit", target="c1", field="status", value="dead"),
        EditOp(file="style.json", op="edit", target="protagonist", value={}),
        EditOp(file="characters.json", op="edit", target="c1", field="nonexist", value=1),
        EditOp(file="nope.json", op="edit", target="x", field="y", value=1),  # 文件白名单外
        EditOp(file="worldstate.json", op="edit", target="timeline", value={}),  # 运行时实然只读
    ):
        with pytest.raises(FeedbackError):
            validate_op(bad)


def test_value_type_guard(tmp_path):
    ws, pid = _ws(tmp_path)
    with pytest.raises(FeedbackError):
        validate_op(EditOp(file="characters.json", op="edit", target="c1",
                           field="personality", value={"a": {"b": 1}}))  # 嵌套脏对象


def test_list_location_and_apply(tmp_path):
    ws, pid = _ws(tmp_path)
    op = EditOp(file="characters.json", op="edit", target="苏晚", field="age", value=20)
    applied = apply_feedback(ws, pid, op)
    assert applied.status == "applied" and applied.applied_at
    data = _read(ws, pid, "bible/characters.json")
    assert data["characters"][0]["age"] == 20

    # 嵌套字段路径
    apply_feedback(ws, pid, EditOp(file="characters.json", op="edit", target="c1",
                                   field="power.level", value="筑基"))
    assert _read(ws, pid, "bible/characters.json")["characters"][0]["power"]["level"] == "筑基"


def test_list_null_apply_skips_sensitive_nested(tmp_path):
    # 定位不到 target 应报错
    ws, pid = _ws(tmp_path)
    with pytest.raises(FeedbackError):
        apply_op(ws, pid, EditOp(file="characters.json", op="edit",
                                 target="不存在的人", field="age", value=1))


def test_add_mints_id_and_delete(tmp_path):
    ws, pid = _ws(tmp_path)
    add = EditOp(file="characters.json", op="add", target="新角色",
                 value={"name": "王五", "age": 30, "personality": "爽朗"}, reason="新增")
    apply_feedback(ws, pid, add)
    data = _read(ws, pid, "bible/characters.json")
    assert len(data["characters"]) == 2
    new = data["characters"][1]
    assert new["name"] == "王五" and new["id"].startswith("char_pf")
    assert new.get("provenance") == "feedback"  # 软 tracking

    dele = EditOp(file="characters.json", op="delete", target="王五")
    apply_feedback(ws, pid, dele)
    assert len(_read(ws, pid, "bible/characters.json")["characters"]) == 1


def test_object_file_edit(tmp_path):
    ws, pid = _ws(tmp_path)
    apply_feedback(ws, pid, EditOp(file="style.json", op="edit", target="tone",
                                   value=["热", "燃"]))
    assert _read(ws, pid, "bible/style.json")["tone"] == ["热", "燃"]


def test_apply_idempotent(tmp_path):
    ws, pid = _ws(tmp_path)
    op = EditOp(file="characters.json", op="edit", target="c1", field="age", value=25)
    apply_feedback(ws, pid, op)
    apply_feedback(ws, pid, op)  # 已 applied 不再重写
    assert _read(ws, pid, "bible/characters.json")["characters"][0]["age"] == 25


def test_sensitive_flags(tmp_path):
    assert flag_sensitive(EditOp(file="worldview.json", op="edit", target="rules", value=[]))
    assert flag_sensitive(EditOp(file="characters.json", op="delete", target="c1"))
    assert flag_sensitive(EditOp(file="worldstate.json", op="edit", target="timeline", value={}))
    assert flag_sensitive(EditOp(file="plot_threads.json", op="edit",
                                 target="t1", field="status", value="resolved"))
    assert not flag_sensitive(EditOp(file="characters.json", op="edit",
                                     target="c1", field="personality", value="x"))


# ---------------------------------------------------------------------------
# 解析器
# ---------------------------------------------------------------------------
def test_parser_splits_ops(tmp_path):
    ws, pid = _ws(tmp_path)
    prov = ScriptedProvider(script=[{"final": json.dumps({
        "ops": [
            {"file": "characters.json", "op": "edit", "target": "苏晚",
             "field": "age", "value": 20, "reason": "改年龄"},
            {"file": "worldview.json", "op": "edit", "target": "rules",
             "value": [], "reason": "改规则"},
        ]}, ensure_ascii=False)}])
    ops = FeedbackParser(prov).parse("意见")
    assert len(ops) == 2
    bytarget = {o.target: o for o in ops}
    assert bytarget["苏晚"].field == "age" and bytarget["苏晚"].value == 20
    # rules 命中 covenant → sensitive
    assert bytarget["rules"].sensitive is True


def test_parser_drops_non_whitelist_and_no_ops(tmp_path):
    ws, pid = _ws(tmp_path)
    prov = ScriptedProvider(script=[{"final": json.dumps({
        "ops": [
            {"file": "nope.json", "op": "edit", "target": "x", "field": "y", "value": 1},
            {"file": "characters.json", "op": "edit", "target": "c1", "field": "id", "value": "oops"},
        ]}, ensure_ascii=False)}])
    ops = FeedbackParser(prov).parse("意见")
    assert ops == []  # 白名单外 + 只读字段 → 全部被确定性丢弃


def test_parser_empty_opinion_raises(tmp_path):
    ws, pid = _ws(tmp_path)
    with pytest.raises(FeedbackError):
        FeedbackParser(ScriptedProvider(script=[{"final": "{}"}])).parse("   ")


def test_parser_retry_on_malformed(tmp_path):
    ws, pid = _ws(tmp_path)
    prov = ScriptedProvider(script=[
        {"final": "根本不是json"},  # 第一次坏
        {"final": json.dumps({"ops": [{"file": "characters.json", "op": "edit",
                                       "target": "c1", "field": "age", "value": 99}]})},
    ])
    ops = FeedbackParser(prov, max_retries=3).parse("意见")
    assert len(ops) == 1 and ops[0].value == 99


def test_parser_blocked_raises(tmp_path):
    ws, pid = _ws(tmp_path)
    prov = ScriptedProvider(script=[{"final": "", "blocked": True, "block_reason": "shutdown"}])
    with pytest.raises(FeedbackError):
        FeedbackParser(prov).parse("意见")


# ---------------------------------------------------------------------------
# 持久化与审批
# ---------------------------------------------------------------------------
def test_store_persistence_and_status(tmp_path):
    ws, pid = _ws(tmp_path)
    store = load_store(ws, pid)
    op = EditOp(file="characters.json", op="edit", target="c1", field="age", value=30)
    op.status = "pending"
    store.add([op])

    store2 = load_store(ws, pid)  # 重新装载（模拟独立进程）
    assert store2.get(op.id).value == 30
    store2.mark(op.id, "applied", applied_at=1.0)
    store3 = load_store(ws, pid)
    assert store3.get(op.id).status == "applied"
    audit = [o for o in store3.list_ops() if o.status == "applied"]
    assert len(audit) == 1  # 审计留痕


def test_queue_and_apply_flow(tmp_path):
    ws, pid = _ws(tmp_path)
    persist = feedback_persist_dir(ws, pid)
    q = ApprovalQueue(persist_dir=persist)
    store = load_store(ws, pid)
    op = EditOp(file="characters.json", op="edit", target="c1", field="age", value=40)
    op.status = "pending"
    store.add([op])
    session = type("S", (), {"agent": "cli"})()
    q.submit(tool="bible_feedback", session=session,
             params={"op_id": op.id, "file": op.file, "op": op.op, "target": op.target},
             reason="test")

    # 独立进程加载应能看见 pending，approve 后能写回
    q2 = ApprovalQueue.load_persisted(persist_dir=persist)
    req = next(r for r in q2.list_pending() if r.params.get("op_id") == op.id)
    assert q2.decide(req.id, allow=True)
    applied = apply_feedback(ws, pid, store.get(op.id))
    store.mark(op.id, "applied", applied_at=applied.applied_at)
    assert _read(ws, pid, "bible/characters.json")["characters"][0]["age"] == 40
    assert store.get(op.id).status == "applied"


# ---------------------------------------------------------------------------
# CLI 接线
# ---------------------------------------------------------------------------
def test_cli_feedback_parse_list_apply_deny(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from novelist import cli as cli_module

    runner = CliRunner()
    ws, pid = _ws(tmp_path)

    ops_json = json.dumps({
        "ops": [{"file": "characters.json", "op": "edit", "target": "苏晚",
                 "field": "personality", "value": "温润", "reason": "改性格"}]},
        ensure_ascii=False)
    # CLI 内部经 _make_cli_provider 取 provider；打桩成 ScriptedProvider 返回意见拆分 JSON
    monkeypatch.setattr(cli_module, "_make_cli_provider",
                        lambda p, **kw: ScriptedProvider(script=[{"final": ops_json}]))

    d = str(ws._abs(pid))
    r = runner.invoke(cli_module.cli, ["feedback", d,
                                       "--provider=scripted", "--opinion=把苏晚改温润一点"])
    assert r.exit_code == 0, r.output
    assert "拆出 1 条修改" in r.output
    m = re.search(r"op (fb:\w+)", r.output)
    assert m, r.output
    op_id = m.group(1)

    r2 = runner.invoke(cli_module.cli, ["feedback", d, "--list"])
    assert r2.exit_code == 0 and op_id in r2.output

    r3 = runner.invoke(cli_module.cli, ["feedback", d, "--apply", op_id])
    assert r3.exit_code == 0, r3.output
    assert "applied" in r3.output
    assert _read(ws, pid, "bible/characters.json")["characters"][0]["personality"] == "温润"

    # deny another op：换一条意见，解析出改名 op 后拒绝，正文不变
    monkeypatch.setattr(cli_module, "_make_cli_provider",
                        lambda p, **kw: ScriptedProvider(script=[{"final": json.dumps({
                            "ops": [{"file": "characters.json", "op": "edit",
                                     "target": "苏晚", "field": "name", "value": "李四"}]},
                            ensure_ascii=False)}]))
    r6 = runner.invoke(cli_module.cli, ["feedback", d,
                                        "--provider=scripted", "--opinion=把苏晚改叫李四"])
    assert r6.exit_code == 0
    before = _read(ws, pid, "bible/characters.json")["characters"][0]["name"]
    r4 = runner.invoke(cli_module.cli, ["feedback", d, "--list"])
    m2 = re.search(r"op (fb:\w+)", r4.output)
    assert m2
    r5 = runner.invoke(cli_module.cli, ["feedback", d, "--deny", m2.group(1)])
    assert r5.exit_code == 0 and "rejected" in r5.output
    assert _read(ws, pid, "bible/characters.json")["characters"][0]["name"] == before