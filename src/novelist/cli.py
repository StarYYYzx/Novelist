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
        "batch_meta": {"current_batch": 0, "batch_base_ref": None},
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
    """跑流水线到指定工序（docs/07 §6.1）。"""
    ws: Workspace = ctx.obj["workspace"]
    if directory:
        ws = Workspace(root=directory)
    ck = Checkpoint(ws)
    try:
        # 找到唯一项目（demo 语义：扫描根下 project.json 的项目）
        root = ws._abs("")
        project_id = _locate_project(root)
        project = ck.restore(project_id)
    except WorkspaceError as e:
        raise click.ClickException(str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise click.ClickException(f"run: {e}") from e
    click.echo(f"run {project.get('id')} -> {to_stage} (current stage: {project.get('pipeline_state')})")


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
