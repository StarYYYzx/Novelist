"""CLI（docs/07 §6.1）。第一形态命令骨架。"""

from __future__ import annotations

import click

from .config import load_config
from .core.errors import NovelistError


@click.group()
@click.option("--config", "config_path", default=None, help="全局配置文件路径")
@click.pass_context
def cli(ctx: click.Context, config_path: str | None) -> None:
    """Novelist — 多 Agent 长篇小说撰写系统。"""
    ctx.ensure_object(dict)
    ctx.obj["config"] = load_config(config_path)


@cli.command()
@click.argument("directory")
def init(directory: str) -> None:
    """新建项目工作区（docs/07 §6.1，UC-01）。"""
    # 脚手架：占位输出，M0 落地目录规约
    click.echo(f"init: {directory} (placeholder)")


@cli.command()
@click.option("--to", "to_stage", default="正文", help="跑流水线到指定工序")
@click.argument("directory")
@click.pass_context
def run(ctx: click.Context, directory: str, to_stage: str) -> None:
    """跑流水线到指定工序（docs/07 §6.1）。"""
    # 脚手架：占位输出，M2 落地
    click.echo(f"run {directory} to {to_stage} (placeholder)")


@cli.command()
@click.argument("directory")
def status(directory: str) -> None:
    """查询进度与统计（docs/07 §6.1）。"""
    click.echo(f"status {directory} (placeholder)")


@cli.command()
@click.argument("directory")
def export(directory: str) -> None:
    """导出发布包（docs/07 §6.1）。"""
    click.echo(f"export {directory} (placeholder)")


@cli.command()
def server() -> None:
    """启动 HTTP 服务（docs/07 §6.2）。"""
    click.echo("server: not implemented yet")


def main() -> None:
    try:
        cli(obj={})
    except NovelistError as e:
        raise click.ClickException(f"{e.code}: {e.message}") from e
