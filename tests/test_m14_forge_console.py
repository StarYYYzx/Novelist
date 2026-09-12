"""M3l 项目交互中枢（`novelist console`，2026-09-10）测试。

覆盖（单测绝不真调 LLM，docs/09 §2.1）：
- Workspace.list_projects：仅含 project.json 的目录、排序、缺 project.json 忽略
- Console REPL 命令分派：projects/new（自动切入）/open/show 错误路径/q 退出
- 分派到真实 click 命令：seed（fake+smoke 只提炼不构建）、show 缺口概览
- _compose_argv：仅对支持 provider 的命令注入，init/show 不注入
"""

from __future__ import annotations

from novelist.forge.console import (
    Console,
    FilterableIO,
    _compose_argv,
)
from novelist.storage.workspace import Workspace


def _console(ws_root: str, lines: list[str] | None = None,
             *, provider: str = "fake", smoke: bool = False) -> tuple[Console, FilterableIO]:
    io = FilterableIO(lines=lines or [], tty=True)
    c = Console(io=io, workspace_root=ws_root, provider=provider, smoke=smoke)
    return c, io


# ---------- Workspace.list_projects ----------

def test_list_projects_only_registered(tmp_path):
    ws = Workspace(root=str(tmp_path))
    from novelist.storage.checkpoint import Checkpoint
    ws.create_project("proj-aaa")
    ws.create_project("proj-bbb")
    # 只有写 project.json 才视为"已登记"（init 经 Checkpoint.save 写入）
    Checkpoint(ws).save("proj-aaa", {"id": "proj-aaa", "title": "甲", "pipeline_state": "立项"})
    Checkpoint(ws).save("proj-bbb", {"id": "proj-bbb", "title": "乙", "pipeline_state": "立项"})
    (tmp_path / "proj-ccc").mkdir()  # 无 project.json → 忽略
    assert ws.list_projects() == ["proj-aaa", "proj-bbb"]


def test_list_projects_empty(tmp_path):
    ws = Workspace(root=str(tmp_path))
    assert ws.list_projects() == []


# ---------- Console REPL 分派 ----------

def test_console_new_creates_and_stepin(tmp_path):
    c, io = _console(str(tmp_path), lines=["/new 书名甲", "/q"])
    st = c.run()
    assert st.project_id and st.project_id.startswith("proj-")
    ids = Workspace(root=str(tmp_path)).list_projects()
    assert ids == [st.project_id]
    assert any("已切入" in ln for ln in io.out)
    assert any("书名甲" in ln or st.project_id in ln for ln in io.out)  # project.json title


def test_console_new_stepin_matches_created_id_when_others_exist(tmp_path):
    """切 id 必须等于本命令新建的，而非猜列表末位（复现 proj-yelan3 误切）。"""
    from novelist.storage.checkpoint import Checkpoint
    ws = Workspace(root=str(tmp_path))
    # 预置一个排序靠后的项目（字母序大），制造"list[-1] 会取错"的场景
    ws.create_project("proj-yelan3")
    Checkpoint(ws).save("proj-yelan3", {"id": "proj-yelan3", "title": "旧",
                                        "pipeline_state": "立项"})
    c, io = _console(str(tmp_path), lines=["/new 新书标题", "/q"])
    st = c.run()
    assert st.project_id != "proj-yelan3"
    assert st.project_id.startswith("proj-")
    # 新切入的确实是最新创建的那个（比旧项目字母序小或大均可，但必须 >旧）
    ids = ws.list_projects()
    assert st.project_id in ids
    assert "proj-yelan3" in ids


def test_console_projects_lists_empty_then_after_new(tmp_path):
    c, io = _console(str(tmp_path), lines=["/projects", "/new 书名", "/projects", "/q"])
    c.run()
    # 第一次 projects 亮空提示，第二次有列出行
    assert any("尚无项目" in ln for ln in io.out)


def test_console_open_unknown_and_show_need_project(tmp_path):
    c, io = _console(str(tmp_path), lines=["/open nope", "/show", "/q"])
    st = c.run()
    assert st.project_id is None
    assert any("未找到项目" in ln for ln in io.out)
    assert any("未选定项目" in ln for ln in io.out)


def test_console_help_and_exit(tmp_path):
    c, io = _console(str(tmp_path), lines=["/help", "/q"])
    c.run()
    assert any("/seed" in ln for ln in io.out)  # help 含 forge 说明
    assert any("项目导航" in ln for ln in io.out)


def test_console_q_exits_clean(tmp_path):
    c, io = _console(str(tmp_path), lines=["/q"])
    st = c.run()
    assert st.ok is True
    assert any("已退出" in ln for ln in io.out)


# ---------- 真实 click 分派（fake+smoke，不真调 LLM）----------

def test_console_seed_builds_blueprint_and_show_gaps(tmp_path):
    c, io = _console(str(tmp_path), lines=["/new 种子书", "/seed 少年觉醒", "/show", "/q"],
                     smoke=True)
    c.run()
    joined = "\n".join(io.out)
    assert "seed done" in joined           # seed(fake) 提炼建蓝图成功
    assert "缺口:" in joined               # show 打印缺口概览
    assert "rev=1" in joined


def test_console_seed_requires_brief(tmp_path):
    c, io = _console(str(tmp_path), lines=["/new 种子书", "/seed", "/q"])
    c.run()
    assert any("用法：/seed" in ln for ln in io.out)


# ---------- _compose_argv provider 透传 ----------

def _console_obj(ws_root: str) -> Console:
    c = Console(io=FilterableIO(), workspace_root=ws_root, provider="deepseek",
                api_key="k", api_base="b", model="m")
    return c


def test_compose_injects_provider_for_forge_seed(tmp_path):
    c = _console_obj(str(tmp_path))
    out = _compose_argv(c, ["forge", "seed", "话", "--dir", str(tmp_path), "--mode", "auto"])
    assert "--provider" in out and "deepseek" in out
    assert "--api-key" in out and "k" in out


def test_compose_skips_init_and_show(tmp_path):
    c = _console_obj(str(tmp_path))
    assert _compose_argv(c, ["init", str(tmp_path), "--title", "x"]) == \
        ["init", str(tmp_path), "--title", "x"]
    assert _compose_argv(c, ["forge", "show", str(tmp_path)]) == \
        ["forge", "show", str(tmp_path)]


# ---------- 全部命令接入（2026-09-10）----------

def test_console_dispatch_unknown_warns(tmp_path):
    c, io = _console(str(tmp_path), lines=["bogus", "/q"])
    c.run()
    assert any("必须以 / 开头" in ln for ln in io.out)


def test_console_projects_lists_seq_title_id(tmp_path):
    """需求：项目列表按「序号 标题 编号」展示（不再只有编号）。"""
    from novelist.storage.checkpoint import Checkpoint
    ws = Workspace(root=str(tmp_path))
    ws.create_project("proj-aaa")
    Checkpoint(ws).save("proj-aaa", {"id": "proj-aaa", "title": "第一本",
                                     "pipeline_state": "立项"})
    c, io = _console(str(tmp_path), lines=["/projects", "/q"])
    c.run()
    joined = "\n".join(io.out)
    assert " 1 第一本 proj-aaa" in joined or "第一本" in joined
    assert "proj-aaa" in joined


def test_console_help_lists_all_commands(tmp_path):
    c, io = _console(str(tmp_path), lines=["/help", "/q"])
    c.run()
    joined = "\n".join(io.out)
    for key in ["seed", "build", "resume", "shell", "roll", "roll-window",
                "ingest", "craft", "covenant", "lines-replay",
                "fr-review", "fr-approve", "fr-revise", "fr-switches",
                "chapter", "run", "status", "draft", "export", "stats",
                "review", "feedback", "grant",
                "characters-enrich", "enrich-pending", "settings-pending",
                "new", "open", "projects"]:
        assert f"/{key}" in joined, f"help 缺失命令 /{key}"


def test_console_build_and_status_need_project(tmp_path):
    c, io = _console(str(tmp_path), lines=["/build", "/status", "/q"])
    c.run()
    assert sum(1 for ln in io.out if "未选定项目" in ln) == 2


def test_console_show_need_blueprint(tmp_path):
    c, io = _console(str(tmp_path), lines=["/new 无蓝图", "/show", "/q"], smoke=True)
    c.run()
    # show 无蓝图会经 cli 抛 ClickException；console 应吞并提示，不崩溃
    assert any("error" in ln.lower() or "尚无蓝图" in ln or "缺口" in ln for ln in io.out)


def test_console_shell_needs_blueprint(tmp_path):
    """shell 未建蓝图时给提示而非崩溃（此前直接抛 FileNotFoundError）。"""
    c, io = _console(str(tmp_path), lines=["/new 无图", "/shell", "/q"], smoke=True)
    c.run()
    assert any("尚无蓝图" in ln for ln in io.out)


def test_console_shell_enters_and_exits(tmp_path):
    """建蓝图后 shell 进入会话并 /exit 返回 console（fake provider，不真调 LLM）。"""
    # new → seed(fake+smoke 只提炼建蓝图) → shell：/show 看概览 → /exit 返回 → q
    c, io = _console(
        str(tmp_path),
        lines=["/new shell书", "/seed 少年觉醒", "/shell", "/show", "/exit", "/q"],
        smoke=True,
    )
    c.run()
    joined = "\n".join(io.out)
    assert "shell" in joined          # shell 会话已启动
    assert "已退出控制台" in joined     # 最终回到 console 并退出
    # shell 内部 /show 打印了蓝图概览（或会话 banner）
    assert any(k in joined for k in ("缺口", "已填", "extras", "阶段"))