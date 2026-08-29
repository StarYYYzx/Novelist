"""CLI 冒烟测试（docs/07 §6.1）：验证命令骨架可被 click 加载并响应。"""

from __future__ import annotations

from click.testing import CliRunner

from novelist.cli import cli


def test_cli_help_smoke():
    runner = CliRunner()
    # 用 --help 触发 click 的默认帮助（不执行任何实现逻辑，纯只读）
    res = runner.invoke(cli, ["--help"])
    assert res.exit_code == 0
    assert "Novelist" in res.output
