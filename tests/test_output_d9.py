"""输出出口（core/output）测试：D9 编码兜底 + U6 耗时格式化。

背景：生成期进度原先裸 `print`，在 console/server 模式会被 `CliRunner` 攒到命令结束
才刷出（长任务期间静默），且非 UTF-8 stdout 下打印中文直接 `UnicodeEncodeError`。
本文件是 `emit` / `use_output` / `fmt_duration` 的守卫。
"""

from __future__ import annotations

import pytest

from novelist.core import output


def test_fmt_duration_minutes_and_seconds():
    assert output.fmt_duration(0) == "00:00"
    assert output.fmt_duration(9) == "00:09"
    assert output.fmt_duration(61) == "01:01"
    assert output.fmt_duration(3599) == "59:59"


def test_fmt_duration_hours_and_negative():
    # 超一小时进位为 h:mm:ss（长章真机会到这一步）
    assert output.fmt_duration(3600) == "1:00:00"
    assert output.fmt_duration(3725) == "1:02:05"
    # 时钟回拨/浮点误差导致的负值不得渲染成 "-1:-1"
    assert output.fmt_duration(-3) == "00:00"


def test_emit_uses_bound_sink_and_restores():
    lines: list[str] = []
    with output.use_output(lines.append):
        output.emit("第一行")
        assert lines == ["第一行"]
    # 退出上下文后 sink 还原：再 emit 不再进 lines（走 stdout）
    output.emit("不进 sink")
    assert lines == ["第一行"]


def test_emit_falls_back_to_stdout_when_sink_raises(capsys):
    def broken_sink(_line: str) -> None:
        raise RuntimeError("sink 故障")

    with output.use_output(broken_sink):
        output.emit("兜底输出")
    assert "兜底输出" in capsys.readouterr().out


def test_emit_survives_unencodable_output(monkeypatch, capsys):
    """D9 核心：stdout 编码不含中文时**降级替换**，而不是把生成打断。"""

    class _AsciiStdout:
        """模拟只接受 ASCII 的 stdout（Windows 重定向 + cp1252 locale 的典型形态）。"""

        encoding = "ascii"

        def __init__(self) -> None:
            self.written: list[str] = []

        def write(self, s: str) -> int:
            s.encode("ascii")  # 编不了就抛 UnicodeEncodeError，与真实流一致
            self.written.append(s)
            return len(s)

        def flush(self) -> None:  # pragma: no cover - 接口占位
            pass

    fake = _AsciiStdout()
    monkeypatch.setattr(output.sys, "stdout", fake)
    output.emit("中文进度不应崩")  # 不抛异常即为通过
    assert fake.written, "兜底路径必须仍写出内容"
    assert all(ch.isascii() for ch in "".join(fake.written))


@pytest.mark.parametrize("seconds,expected", [(125, "02:05"), (7325, "2:02:05")])
def test_fmt_duration_roundtrip(seconds, expected):
    assert output.fmt_duration(seconds) == expected
