"""问答通道接口 + 终端实现（docs/10 §2：接口化，日后换 HTTP 不动引擎）。

引擎侧（`forge/ask.py`）只依赖 `AnswerIO` 协议；`ConsoleIO` 是当前唯一的
终端实现。非 TTY（CI/管道/测试）时 `is_tty=False`，引擎自动降级为全取推荐值
（docs/10 §5.3：interactive 非交互环境降级 auto + 写 transcript）。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any, Protocol


class AnswerIO(Protocol):
    """问答通道接口（引擎侧依赖面，F2）。"""

    @property
    def is_tty(self) -> bool: ...

    def notify(self, text: str) -> None:
        """打印信息（不要求回答）。"""

    def ask_choice(self, prompt: str, options: list[str], default_idx: int = 0) -> int | None:
        """单选：返回选项序号（0 起）；None=用户 q 退出。回车=default_idx。"""

    def ask_free(self, prompt: str, default: str = "") -> str | None:
        """自由输入：返回文本（回车=default）；None=q 退出。"""

    def confirm(self, prompt: str, default: bool = True) -> bool:
        """是/否确认。"""


def _clean(text: str) -> str:
    return text.strip()


@dataclass
class ConsoleIO:
    """终端实现（stdin/stdout 可注入以便测试）。"""

    _in: Any = field(default_factory=lambda: sys.stdin)
    _out: Any = field(default_factory=lambda: sys.stdout)

    @property
    def is_tty(self) -> bool:
        try:
            return bool(self._in.isatty() and self._out.isatty())
        except Exception:  # noqa: BLE001 - 注入的流可能没有 isatty
            return False

    def notify(self, text: str) -> None:
        print(text, file=self._out, flush=True)

    def _read(self) -> str:
        try:
            return self._in.readline()
        except EOFError:
            return ""

    def ask_choice(self, prompt: str, options: list[str], default_idx: int = 0) -> int | None:
        default_idx = min(max(default_idx, 0), max(len(options) - 1, 0))
        lines = [prompt]
        for i, opt in enumerate(options, 1):
            mark = " ← 推荐" if i - 1 == default_idx else ""
            lines.append(f"  ({i}) {opt}{mark}")
        lines.append("回车=推荐 | 输入编号 | q=退出：")
        print("\n".join(lines), file=self._out, flush=True)
        raw = _clean(self._read())
        if raw.lower() == "q":
            return None
        if not raw:
            return default_idx
        try:
            n = int(raw)
        except ValueError:
            print(f"  无法识别 {raw!r}，按推荐值取。", file=self._out, flush=True)
            return default_idx
        if 1 <= n <= len(options):
            return n - 1
        print(f"  编号 {n} 超出范围（1-{len(options)}），按推荐值取。", file=self._out, flush=True)
        return default_idx

    def ask_free(self, prompt: str, default: str = "") -> str | None:
        hint = f"（回车=默认：{default}）" if default else ""
        print(f"{prompt} {hint}", file=self._out, flush=True)
        raw = self._read()
        if raw.strip().lower() == "q":
            return None
        return raw.strip() if raw.strip() else default

    def confirm(self, prompt: str, default: bool = True) -> bool:
        tag = "y/n" if default else "n/y"
        print(f"{prompt} [{tag}]：", file=self._out, flush=True)
        raw = _clean(self._read()).lower()
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        return default
