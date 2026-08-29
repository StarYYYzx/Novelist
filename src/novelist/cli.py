"""CLI（docs/07 §6.1）。命令骨架正在逐步真实化（M0：init）。"""

from __future__ import annotations

import click

from .config import load_config
from .core.errors import NovelistError
from .storage.checkpoint import Checkpoint
from .storage.workspace import WorkspaceError, Workspace


@click.group()
@click.option("--config", "config_path", default=None, help="全局配置文件路径")
@click.pass_context
def cli(ctx: click.Context, config_path: str | None) -> None:
    """Novelist — 多 Agent 长篇小说撰写系统。"""
    ctx.ensure_object(dict)
    cfg = load_config(config_path)
    ctx.obj["config"] = cfg
    # workspace 根：默认当前目录；可用配置项 storage.workspace_root 覆盖
    root = cfg.storage.workspace_root or "."
    ctx.obj["workspace"] = Workspace(root=root)


def _new_project_id() -> str:
    import time

    return "proj-" + time.strftime("%Y%m%d%H%M%S")


@cli.command()
@click.argument("directory", required=False, default=None)
@click.option("--title", default=None, help="书名")
@click.pass_context
def init(ctx: click.Context, directory: str | None, title: str | None) -> None:
    """新建项目工作区（docs/07 §6.1，UC-01）。"""
    ws: Workspace = ctx.obj["workspace"]
    project_id = _new_project_id()
    if directory:
        # 允许在指定目录下创建项目子目录
        ws = Workspace(root=directory)
    try:
        ws.create_project(project_id)
    except WorkspaceError as e:
        raise click.ClickException(str(e)) from e

    project = {
        "id": project_id,
        "title": title or project_id,
        "brief": "",
        "genre": None,
        "prefs": {},
        "budget": {},
        "pipeline_state": "立项",
        "event_seq": 0,
    }
    ck = Checkpoint(ws)
    ck.save(project_id, project)
    click.echo(f"created project {project_id} at {ws.project_dir(project_id)}")
    click.echo(f"  stage: {project['pipeline_state']}")


@cli.command()
@click.argument("directory", required=False, default=None)
@click.option("--to", "to_stage", default="正文", help="跑流水线到指定工序")
@click.pass_context
def run(ctx: click.Context, directory: str | None, to_stage: str) -> None:
    """跑流水线到指定工序（docs/07 §6.1）。严格串行逐章；审查阶段触发一致性检查。"""
    from novelist.core.pipeline import PipelineStateMachine, PipelineStateError, PIPELINE_STAGES
    from novelist.consistency import run_consistency

    ws: Workspace = ctx.obj["workspace"]
    if directory:
        ws = Workspace(root=directory)
    ck = Checkpoint(ws)
    try:
        root = ws._abs("")
        project_id = _locate_project(root)
        project = ck.restore(project_id)
    except WorkspaceError as e:
        raise click.ClickException(str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise click.ClickException(f"run: {e}") from e

    try:
        st = PipelineStateMachine()
        cur = project.get("pipeline_state") or "立项"
        want = to_stage if to_stage in PIPELINE_STAGES else "正文"
        # 先推进到当前已存阶段（若存在），再从当前推进到目标
        if cur != st.current:
            _advance_to(st, cur)
        if want != st.current:
            _advance_to(st, want)
    except PipelineStateError:
        raise click.ClickException(
            f"cannot advance {project.get('pipeline_state')} -> {to_stage}（串行推进，仅允许前进）"
        ) from None

    # 审查及以上：执行一致性规则检查并报告
    alerts = []
    if _stage_ord(want) >= _stage_ord("审查"):
        alerts = run_consistency(ws, project_id)

    project["pipeline_state"] = st.current
    try:
        ck.save(project_id, project)
    except Exception as e:  # noqa: BLE001
        raise click.ClickException(f"run save: {e}") from e

    click.echo(f"run {project.get('id')} -> {st.current}")
    if alerts:
        n_block = sum(1 for a in alerts if a.level == "block")
        click.echo(f"  consistency: {len(alerts)} alert(s), {n_block} block")
        for a in alerts[:5]:
            click.echo(f"    [{a.level}/{a.rule_id}] {a.object_ref}: {a.detail}")
    else:
        click.echo("  consistency: ok")


def _stage_ord(name: str) -> int:
    from novelist.core.pipeline import PIPELINE_STAGES

    if name in PIPELINE_STAGES:
        return PIPELINE_STAGES.index(name)
    return -1


def _advance_to(st, target: str) -> None:
    """逐步把状态机推进到 target（每次前进一步，直至目标）。"""
    while st.current != target:
        stages = st.stages
        i = stages.index(st.current)
        nxt = stages[i + 1]
        st.advance(nxt)


@cli.command()
@click.argument("directory", required=False, default=None)
@click.pass_context
def status(ctx: click.Context, directory: str | None) -> None:
    """查询进度与统计（docs/07 §6.1）。"""
    ws: Workspace = ctx.obj["workspace"]
    if directory:
        ws = Workspace(root=directory)
    ck = Checkpoint(ws)
    root = ws._abs("")
    project_id = _locate_project(root)
    try:
        project = ck.restore(project_id)
    except Exception as e:  # noqa: BLE001
        raise click.ClickException(f"status: {e}") from e
    click.echo(f"project: {project.get('id')}")
    click.echo(f"title:   {project.get('title')}")
    click.echo(f"stage:   {project.get('pipeline_state')}")


@cli.command()
@click.argument("directory", required=False, default=None)
@click.option("--vol", default=1, type=int, help="卷号")
@click.option("--ch", default=1, type=int, help="章节号")
@click.option("--provider", default="fake", help="LLM provider：fake/scripted/openai")
@click.pass_context
def chapter(ctx: click.Context, directory: str | None, vol: int, ch: int, provider: str) -> None:
    """串行写一章：主编剧经 Agent 循环调工具产出草稿 + 事件实时回写（docs/04 §4.1 / ADR-013）。"""
    from novelist.core.orchestrator import produce_chapter

    ws: Workspace = ctx.obj["workspace"]
    if directory:
        ws = Workspace(root=directory)
    ck = Checkpoint(ws)
    root = ws._abs("")
    try:
        project_id = _locate_project(root)
        ck.restore(project_id)
    except Exception as e:  # noqa: BLE001
        raise click.ClickException(f"chapter: {e}") from e

    prov = _make_cli_provider(provider, vol, ch)
    res = produce_chapter(ws, project_id, vol, ch, prov)
    if not res.ok:
        raise click.ClickException(f"chapter production failed: {res.result}")
    click.echo(f"wrote draft: {res.chapter_path}")
    click.echo(f"events committed: {res.events_committed}")


def _make_cli_provider(provider: str, vol: int = 1, ch: int = 1):
    from novelist.providers.fake import FakeProvider, ScriptedProvider

    if provider == "fake":
        # 占位演示：返回一段文本（不产生工具调用），展示循环结束
        return FakeProvider(reply="演示：fake provider 直接返回文本。")
    if provider in ("scripted", "demo"):
        # 默认/演示：脚本驱动一次 write_draft 工具调用（写入指定卷/章）+ 结束语
        return ScriptedProvider(
            [
                {"tool": "write_draft", "args": {"vol": vol, "ch": ch, "content": f"第 {ch} 章占位草稿：由 scripted provider 写入。"}},
                {"final": "done"},
            ]
        )
    if provider == "deepseek":
        from novelist.providers.deepseek import DeepSeekProvider

        return DeepSeekProvider(model="deepseek-chat")
    # openai 等真实 provider（需 key/base_url，见 providers.openai）
    from novelist.providers.openai import OpenAICompatibleProvider

    return OpenAICompatibleProvider(model="gpt-4o-mini")


@cli.command()
@click.argument("directory", required=False, default=None)
@click.pass_context
def export(ctx: click.Context, directory: str | None) -> None:
    """导出发布包（docs/07 §6.1）。"""
    click.echo("export: not implemented yet (M3)")


@cli.command()
@click.pass_context
def server(ctx: click.Context) -> None:
    """启动 HTTP 服务（docs/07 §6.2）。"""
    click.echo("server: not implemented yet (M3)")


def _locate_project(root) -> str:
    """在当前根下定位唯一项目 id（首层子目录中含 project.json 的）。"""
    import pathlib

    rp = pathlib.Path(root)
    found = [d.name for d in rp.iterdir() if d.is_dir() and (d / "project.json").exists()]
    if not found:
        raise click.ClickException("no project found; run `novelist init` first")
    if len(found) > 1:
        raise click.ClickException(f"multiple projects found: {found}; target one explicitly")
    return found[0]


def main() -> None:
    try:
        cli(obj={})
    except NovelistError as e:
        raise click.ClickException(f"{e.code}: {e.message}") from e


if __name__ == "__main__":
    main()
