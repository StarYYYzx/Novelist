"""项目交互中枢（`novelist console`，2026-09-10）。

一行指令启动 → 进入常驻 REPL，集项目导航与完整新建/编辑/构建于一体。

**命令面（全部接入 CLI 指令，2026-09-10）**
- 项目导航：`projects` / `ls` / `new <标题>` / `open <id>`
- 构建链（forge）：`seed` `build` `resume` `shell` `roll` `roll-window`
  `ingest` `forge-validate` `show` `craft` `covenant` `lines-replay`
  `rollback` `snapshots` `fr-review` `fr-approve` `fr-approve-all` `fr-revise` `fr-switches`
- 正文/流水线（顶层）：`chapter` `run` `status` `draft` `export`
- 审核/一致性（顶层）：`review`（正文一致性）`feedback` `grant`
- 统计与设定：`stats` `validate`（顶层欠约束检查，**与 `forge-validate` 不同**）
- 纪要：`characters-enrich` `enrich-pending` `settings-pending` `settings-allow`
- 对话（M3ac，2026-09-19）：**非 `/` 输入 = 自然语对话**（主编剧 agent，自主调
  只读工具查证、可写草稿；写受控路径当场审批）；`agent` 看会话状态
- 帮助/退出：`help` `exit` / `quit` / `q`

**参数定位纪律（D7，与 cli.py 逐字对齐，勿再混用）**
- 位置参数 `[DIRECTORY]`：build/roll/roll-window/resume/shell/show/craft/covenant/
  validate/rollback/snapshots/ingest(SOURCE 后)/lines-replay，及顶层 run/status/
  review/feedback/grant/export/stats/draft/characters-enrich/enrich-pending/
  settings-pending/chapter(--vol/--ch)
- `--dir` option：seed、fr-review/fr-approve/fr-approve-all/fr-revise/fr-switches
- `--vol`/`--ch` option：chapter、lines-replay
- provider 透传仅对接收 `--provider` 的命令注入（见 `_PROVIDER_CMDS`），
  其余（run/status/craft/show/covenant/validate/rollback 等）不注入，避免多余 flag。

设计：**不复制业务逻辑**——每个命令经 `CliRunner.invoke` 复调现有 click 命令，
保证与命令行行为逐字一致。命令输出转发到 io。provider 参数在启动时传入，
统一经 `_make_cli_provider` 解析（DeepSeek 默认，env/flag 覆盖）。
"""

from __future__ import annotations

import builtins
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any

import click
import contextlib

from ..cli import cli as cli_group
from ..core.output import use_output
from ..storage.workspace import Workspace


def _io_decision(io: FilterableIO):
    """门禁审批的 REPL 通道（M3ac-1）：sensitive 工具调用当场问用户。

    与 cli.py `_interactive_decision` 同语义，但走 console 的 FilterableIO
    （CliRunner 隔离环境里 click.prompt 接触不到真实 stdin）。
    """

    def _decide(req) -> str:
        io.output(f"[approval] 工具 {req.tool} 请求执行：{req.reason}")
        io.output(f"  参数：{str(req.params)[:200]}")
        ans = io.ask_free("  允许？[y/N]")
        return "allow" if (ans or "").strip().lower() in ("y", "yes") else "deny"

    return _decide


@dataclass
class ConsoleState:
    ok: bool = True
    project_id: str | None = None
    commands_run: int = 0
    logged: list[str] = field(default_factory=list)

    def log(self, line: str) -> None:
        self.logged.append(line)


# —— REPL 输入原语：薄封装，便于测试注入 ——

class _CliStream:
    """把 click 的 stdout 调用**逐行即时**转发到 REPL io，并累积全文备用（D-14）。"""

    def __init__(self, io_) -> None:
        self._io = io_
        self._buf: list[str] = []
        self._pending = ""

    def write(self, text) -> int:
        if not text:
            return 0
        if isinstance(text, (bytes, bytearray)):  # 有调用方向 stdout 写 bytes（如 bytes 路径回显）
            text = bytes(text).decode("utf-8", "replace")
        self._buf.append(text)
        self._pending += text
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            if line.strip():
                self._io.output(line)
        return len(text)

    def flush_line(self) -> None:
        if self._pending.strip():
            self._io.output(self._pending)
        self._pending = ""

    def flush(self) -> None:  # 兼容 TextIO 协议
        return None

    def text(self) -> str:
        return "".join(self._buf)


class _CliRun:
    """`run_cli` 的返回壳（保持 `.output` / `.exit_code` / `.exception` 兼容）。"""

    def __init__(self, output: str, exit_code: int, exception: object | None) -> None:
        self.output = output
        self.exit_code = exit_code
        self.exception = exception


class FilterableIO:
    """可注入的 REPL 通道（脚本化输入 / 捕获输出），并承担 `q` 之外的中断。

    is_tty 控制是否执行逐键交互；非 TTY 下默认命令仍会走完整的 click 调用
    （LLM 调用由 `run` 层统一按 provider 处理，此层不碰 LLM）。
    """

    def __init__(self, lines: list[str] | None = None, *, tty: bool = True) -> None:
        self._lines = list(lines or [])
        self._tty = tty
        self.out: list[str] = []
        self.asks: list[str] = []

    @property
    def is_tty(self) -> bool:
        return self._tty

    def input(self, prompt: str) -> str:
        self.asks.append(prompt)
        if self._lines:
            return self._lines.pop(0)
        # 无脚本输入：tty 下阻塞读真实键盘（REPL 实际交互）；非 tty 视同结束
        if self._tty:
            try:
                value = builtins.input(prompt + " ")
            except (EOFError, KeyboardInterrupt):
                return ""
            return value or ""
        return ""

    def output(self, text: str) -> None:
        self.out.append(text)
        # 真实终端（tty 且非脚本注入）直接打印——否则 REPL 全程静默。
        # 注意：本方法同时是 `emit()` 的 sink（见 run_cli），**不得回调 emit**（自递归）。
        if self._tty and not self._lines:
            try:
                print(text, flush=True)
            except UnicodeEncodeError:  # D9：编码不含中文时退化为可编码形式
                enc = getattr(sys.stdout, "encoding", None) or "utf-8"
                print(text.encode(enc, "replace").decode(enc, "replace"), flush=True)

    def notify(self, text: str) -> None:
        self.output(text)

    def ask_free(self, prompt: str, default: str = "") -> str | None:
        """AnswerIO 自由输入入口（shell 复用）；语义= console 的一行输入。"""
        if default and not self._lines and not self._tty:
            self.output(f"（回车=默认：{default}）")
        line = self.input(prompt)
        if line == "" and default and not self._lines:
            return default
        return line if line is not None else None

    # —— 兼作 AnswerIO（供 run_shell 复用当前进程交互通道）——
    # 这样 console 内嵌 shell 时，shell 与 console 共享同一读写通道，
    # 不经过 CliRunner 隔离环境（否则 is_tty 失真 / stdin 被隔断）。

    def ask_choice(self, prompt: str, options: list[str], default_idx: int = 0) -> int | None:
        lines = [prompt]
        for i, opt in enumerate(options, 1):
            lines.append(f"  ({i}) {opt}")
        lines.append("回车=默认 | 编号 | q=退出：")
        self.output("\n".join(lines))
        raw = self.input("").strip().lower()
        if raw == "q":
            return None
        if not raw:
            return default_idx
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        return default_idx

    def confirm(self, prompt: str, default: bool = True) -> bool:
        tag = "y/n" if default else "n/y"
        self.output(f"{prompt} [{tag}]：")
        raw = self.input("").strip().lower()
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        return default


# 极简命令注册表。处理器签名：def handler(cmd: Console, args: str) -> None
# args 为原始参数字符串（未分词），便于保留带引号内容；处理器内 self.run_cli(argv)。


def _default_workspace_root() -> str:
    return os.environ.get("NOVELIST_WORKSPACE", "") or "novel_workspace"


class Console:
    """`novelist console` 交互中枢。"""

    def __init__(
        self,
        *,
        io: FilterableIO,
        workspace_root: str | None = None,
        provider: str = "deepseek",
        api_key: str | None = None,
        api_base: str | None = None,
        model: str | None = None,
        smoke: bool = False,
    ) -> None:
        self.io = io
        self.ws_root = workspace_root or _default_workspace_root()
        self.provider = provider
        self.api_key = api_key
        self.api_base = api_base
        self.model = model
        self.smoke = smoke
        self.state = ConsoleState()
        self._ws = Workspace(root=self.ws_root)
        # M3ac-1（ADR-036）：对话 agent 按项目惰性创建、跨命令持有（会话内连续对话）
        self._chat_agents: dict[str, Any] = {}

    # —— 工具 ——

    def _proj_dir(self, pid: str) -> str:
        return (self._ws.project_dir(pid)).as_posix()

    def _project_loc(self) -> str | None:
        return self._proj_dir(self.state.project_id) if self.state.project_id else None

    def _need_project(self) -> bool:
        if self.state.project_id is None:
            self.io.output("[!] 未选定项目：`/open <id>` 或 `/new <标题>` 先建/选一个。")
            return False
        return True

    def run_cli(self, argv: list[str]):
        """复调 click CLI，输出转发到 io（自动缝合 provider 透传参数）。

        返回 click TestResult，便于调用方解析（如 `new` 从 `created project <id>`
        精确拿到新建项目 id，而非猜列表末位）。
        """
        full = _compose_argv(self, argv)
        # U3（2026-09-15）：原用 `catch_exceptions=False`——此时 click **直接抛出**，
        # `r.exception` 永远是 None（下面那行是死代码），而 dispatch/run 都没有 try/except
        # → 任一命令失败（/chapter 无蓝图、/show 坏路径、/open 权限…）会**终止整个 REPL
        # 并抛栈**，用户丢会话。改为 True：异常进 `r.exception`，转成一行提示，会话继续。
        # U6：同时把生成期进度接到 io（`emit` 走 sink）——原先这些输出被 CliRunner 捕获，
        # 要等命令结束才一次性刷出，长任务期间全程静默。
        # 2026-09-19 决策 D-14：改用流式转发——CliRunner 把 click.echo 全量捕获到
        # 命令结束才一次性刷出（`emit` 的进度行却是实时的，两路输出分裂；长任务期间
        # 用户看到的是"进度在跳、结果卡住"）。这里把 stdout 接到 io，**逐行即时输出**，
        # 同时累积一份文本供调用方解析（如 `new` 从 "created project <id>" 取 id）。
        stream = _CliStream(self.io)
        with use_output(self.io.output), contextlib.redirect_stdout(stream):
            code, exc = 0, None
            try:
                cli_group.main(args=full, prog_name="novelist", standalone_mode=False)
            except SystemExit as e:  # 命令内部 sys.exit
                code = int(getattr(e, "code", 0) or 0)
            except click.ClickException as e:  # click 用法/业务错误
                self.io.output(f"[error] {e.format_message()}")
                code, exc = 1, e
            except click.Abort:
                self.io.output("[aborted] 已中断")
                code = 1
            except Exception as e:  # noqa: BLE001 - 任一命令失败不得终止 REPL
                self.io.output(f"[error] {getattr(e, 'message', e)}")
                code, exc = 1, e
        stream.flush_line()
        return _CliRun(stream.text(), code, exc)

    def _proj_run(self, forge_sub: str, positionals: list[str] = ()) -> bool:
        """以位置参数 [DIRECTORY] 定位项目，调 `forge <sub> [args] [DIRECTORY]`。"""
        if not self._need_project():
            return False
        self.run_cli(["forge", forge_sub, *positionals, self._project_loc()])
        return True

    def _make_provider(self):
        """实例化 LLM provider（复用 cli 的 P0-2 单轨工厂）。"""
        from ..cli import _make_cli_provider

        return _make_cli_provider(
            self.provider,
            api_key=self.api_key,
            api_base=self.api_base,
            model=self.model,
        )

    # —— 命令处理 ——

    def cmd_projects(self, args: str) -> None:
        ids = self._ws.list_projects()
        if not ids:
            self.io.output("（尚无项目——`/new <标题>` 新建。）")
            return
        rows = []
        for i, pid in enumerate(ids, 1):
            title, stage = self._meta(pid)
            rows.append(f"  {i:<3} {title or '（无题）':<32} {pid}")
        self.io.output(f"共 {len(ids)} 个项目（序号 标题 编号）：\n" + "\n".join(rows))

    def _meta(self, pid: str) -> tuple[str, str]:
        pj = self._ws.project_json_path(pid)
        try:
            data = self._ws.read_json(pid, pj, required=False)
        except Exception:  # noqa: BLE001
            return "", ""
        if not isinstance(data, dict):
            return "", ""
        return data.get("title", ""), data.get("pipeline_state", "")

    def cmd_new(self, args: str) -> None:
        title = args.strip() or None
        r = self.run_cli(["init", self.ws_root, "--title", title or "（无题）"])
        # 从 init 输出精确解析新建项目 id（勿猜 list 末位，排序可能把别项目排后面）
        m = re.search(r"created project (\S+)", r.output or "")
        if m:
            pid = m.group(1)
            self.state.project_id = pid
            self.io.output(f"=> 已切入 {pid}")
        else:
            self.io.output("[!] 未能解析新建项目 id——用 `/projects` 查看")

    def cmd_open(self, args: str) -> None:
        pid = args.strip()
        if not pid:
            self.io.output("[!] 用法：/open <id>")
            return
        if pid not in self._ws.list_projects():
            self.io.output(f"[!] 未找到项目 {pid!r}（`/projects` 查看）")
            return
        self.state.project_id = pid
        title, stage = self._meta(pid)
        self.io.output(f"=> {pid}（{title or ''}，stage {stage or '—'}）")

    def cmd_show(self, args: str) -> None:
        self._proj_run("show")

    def cmd_seed(self, args: str) -> None:
        brief = args.strip()
        if not brief:
            self.io.output('[!] 用法：/seed "一句话创意"')
            return
        if not self._need_project():
            return
        argv = ["forge", "seed", brief, "--dir", self._project_loc(), "--mode", "interactive"]
        if self.smoke:
            argv.append("--smoke")
        self.run_cli(argv)

    def cmd_build(self, args: str) -> None:
        """/build [--max-calls N]：构建（预算可显式给；缺省按规模推导）。

        2026-09-16 UX：此前无法指定预算，用户只看到 `[10/234]` 里的 234（推导值）
        却不知道它是什么、也不能改。
        """
        extra: list[str] = []
        toks = args.strip().split()
        for i, t in enumerate(toks):
            if t in ("--max-calls", "-m") and i + 1 < len(toks) and toks[i + 1].isdigit():
                extra += ["--max-calls", toks[i + 1]]
            elif t.startswith("--max-calls=") and t.split("=", 1)[1].isdigit():
                extra += ["--max-calls", t.split("=", 1)[1]]
        self._proj_run("build", extra)

    def cmd_resume(self, args: str) -> None:
        self._proj_run("resume")

    def cmd_conflicts(self, args: str) -> None:
        """/conflicts：列出待裁决的结构冲突（主线冲突 / 伏笔近重复）。"""
        self._proj_run("conflicts")

    def cmd_resolve(self, args: str) -> None:
        """/resolve <id> <choice>：裁决一条冲突（选项见 /conflicts）。"""
        parts = args.strip().split()
        if len(parts) != 2:
            self.io.output("[!] 用法：/resolve <冲突id> <选项>（选项见 /conflicts）")
            return
        self._proj_run("conflicts", ["--resolve", parts[0], parts[1]])

    def cmd_shell(self, args: str) -> None:
        # shell 是常驻会话，不能经 CliRunner 转录（会失去交互通道）；
        # 直接在当前进程内运行，复用 console 的 io 作为 AnswerIO。
        if not self._need_project():
            return
        from ..forge.shell import run_shell
        from .state import Blueprint
        from .slots import default_slots
        from ..core.llm import LLMMessage  # noqa: F401 触发 provider 导入链

        ws = self._ws
        pid = self.state.project_id
        try:
            bp = Blueprint.load(ws, pid)
        except FileNotFoundError:
            self.io.output(
                f"[!] {pid}: 尚无蓝图（blueprint.json）——先 `seed \"一句话\"`。")
            return
        provider = self._make_provider()
        if provider is None:
            return
        run_shell(ws, pid, bp, provider=provider, io=self.io,
                  slots=default_slots())

    def cmd_roll(self, args: str) -> None:
        vol = args.strip().split()[0] if args.strip() else None
        if not vol or not vol.isdigit():
            self.io.output("[!] 用法：/roll <卷号>")
            return
        self._proj_run("roll", [vol])

    def cmd_roll_window(self, args: str) -> None:
        vol = args.strip().split()[0] if args.strip() else None
        if not vol or not vol.isdigit():
            self.io.output("[!] 用法：/roll-window <宽>")
            return
        self._proj_run("roll-window", [vol])

    def cmd_ingest(self, args: str) -> None:
        parts = args.strip().split()
        if not parts:
            self.io.output("[!] 用法：/ingest <源目录/文件>")
            return
        source = parts[0]
        if not self._need_project():
            return
        # source 在前，directory 位置在后：forge ingest SOURCE [DIRECTORY]
        self.run_cli(["forge", "ingest", source, self._project_loc()])

    def cmd_craft(self, args: str) -> None:
        self._proj_run("craft")

    def cmd_covenant(self, args: str) -> None:
        self._proj_run("covenant")

    def cmd_lines_replay(self, args: str) -> None:
        parts = args.strip().split()
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
            self.io.output("[!] 用法：/lines-replay <卷> <章>")
            return
        if not self._need_project():
            return
        self.run_cli(["forge", "lines-replay",
                      "--vol", parts[0], "--ch", parts[1], self._project_loc()])

    def cmd_fr_review(self, args: str) -> None:
        module = args.strip()
        if not self._need_project():
            return
        argv = ["forge", "review"]
        if module:
            argv.append(module)
        argv += ["--dir", self._project_loc()]
        self.run_cli(argv)

    def cmd_fr_approve(self, args: str) -> None:
        parts = args.strip().split()
        if not parts:
            self.io.output("[!] 用法：/fr-approve <模块|all> [--remember]")
            return
        if not self._need_project():
            return
        # 2026-09-19 修复：此前 --remember 写在用法里却不透传（flag 被吃掉）
        extra = [p for p in parts[1:] if p.startswith("--")]
        self.run_cli(["forge", "approve", parts[0], *extra, "--dir", self._project_loc()])

    def cmd_fr_approve_all(self, args: str) -> None:
        parts = args.strip().split()
        if not parts:
            self.io.output("[!] 用法：/fr-approve-all <模块|all> [--off]  "
                           "（批准待审并永久关闭审核；--off 恢复审核）")
            return
        if not self._need_project():
            return
        extra = [p for p in parts[1:] if p.startswith("--")]
        self.run_cli(["forge", "approve-all", parts[0], *extra, "--dir", self._project_loc()])

    def cmd_fr_revise(self, args: str) -> None:
        module = args.strip()
        if not module:
            self.io.output('[!] 用法：/fr-revise <模块> "<修改建议>"')
            return
        if not self._need_project():
            return
        self.run_cli(["forge", "revise", module, args, "--dir", self._project_loc()])

    def cmd_fr_switches(self, args: str) -> None:
        if not self._need_project():
            return
        parts = args.strip().split()
        argv = ["forge", "switches", *parts, "--dir", self._project_loc()]
        self.run_cli(argv)

    def cmd_chapter(self, args: str) -> None:
        parts = args.strip().split()
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
            self.io.output("[!] 用法：/chapter <卷> <章>")
            return
        if not self._need_project():
            return
        self.run_cli(["chapter", self._project_loc(), "--vol", parts[0], "--ch", parts[1]])

    def cmd_run(self, args: str) -> None:
        if not self._need_project():
            return
        argv = ["run", self._project_loc()]
        to = args.strip()
        if to:
            argv += ["--to", to]
        self.run_cli(argv)

    def cmd_run_status(self, args: str) -> None:
        if not self._need_project():
            return
        self.run_cli(["status", self._project_loc()])

    def cmd_draft(self, args: str) -> None:
        if not self._need_project():
            return
        argv = ["draft", self._project_loc()]
        chap = args.strip()
        if chap:
            argv.append(chap)
        self.run_cli(argv)

    def cmd_review(self, args: str) -> None:
        if not self._need_project():
            return
        self.run_cli(["review", self._project_loc()])

    def cmd_export(self, args: str) -> None:
        if not self._need_project():
            return
        self.run_cli(["export", self._project_loc()])

    def cmd_stats(self, args: str) -> None:
        if not self._need_project():
            return
        self.run_cli(["stats", self._project_loc()])

    def cmd_validate_top(self, args: str) -> None:
        if not self._need_project():
            return
        self.run_cli(["validate", self._project_loc()])

    def cmd_contract(self, args: str) -> None:
        """forge 契约校验（V1–V6）。

        U5（2026-09-15）：此前 `/validate` 在帮助里出现两次、描述互斥，且真正的
        `forge validate` 在 console 内**无法调用**（dispatch 只映射到顶层 validate）。
        现拆成两条命令：`/validate` = 顶层欠约束检查，`/forge-validate` = 本命令。
        """
        self._proj_run("validate")

    def cmd_feedback(self, args: str) -> None:
        if not self._need_project():
            return
        self.run_cli(["feedback", self._project_loc()])

    def cmd_grant(self, args: str) -> None:
        if not self._need_project():
            return
        self.run_cli(["grant", self._project_loc()])

    def cmd_chars_enrich(self, args: str) -> None:
        if not self._need_project():
            return
        self.run_cli(["characters-enrich", self._project_loc()])

    def cmd_enrich_pending(self, args: str) -> None:
        if not self._need_project():
            return
        self.run_cli(["enrich-pending", self._project_loc()])

    def cmd_settings_pending(self, args: str) -> None:
        if not self._need_project():
            return
        argv = ["settings-pending", self._project_loc()]
        allow = args.strip()
        if allow:
            argv += ["--allow", allow]
        self.run_cli(argv)

    def cmd_help(self, args: str) -> None:
        self.io.output(HELP_TEXT)

    # —— 对话 agent（ADR-036 / M3ac-1）——

    def _chat(self, line: str) -> None:
        """自然语通道：非 / 输入 → 主编剧对话 agent（多轮工具循环后交付回答）。"""
        if not self._need_project():
            return
        from ..core.agent_chat import ChatAgent

        pid = self.state.project_id
        agent = self._chat_agents.get(pid)
        if agent is None:
            agent = ChatAgent(self._ws, pid, self._make_provider(),
                              decision_fn=_io_decision(self.io))
            self._chat_agents[pid] = agent
        with use_output(self.io.output):
            try:
                answer = agent.ask(line)
            except Exception as e:  # noqa: BLE001 - 对话失败不杀 REPL
                self.io.output(f"[agent error] {type(e).__name__}: {e}")
                return
        self.io.output(answer)

    def cmd_agent(self, args: str) -> None:
        """/agent [status]：查看对话 agent 的会话状态（轮数/用量/成本）。"""
        agent = self._chat_agents.get(self.state.project_id) if self.state.project_id else None
        if agent is None:
            self.io.output("[agent] 尚未开始对话——直接输入自然语即开始（先 /open 选定项目）。")
            return
        self.io.output(agent.status_line())

    def cmd_exit(self, args: str) -> bool:
        return True

    def dispatch(self, line: str) -> bool:
        """分发一行输入；返回 True 表示应退出 REPL。"""
        line = line.strip()
        if not line:
            return False
        if not line.startswith("/"):
            # M3ac-1（ADR-036，2026-09-19）：非 / 输入 = 自然语对话，喂给对话 agent
            # （此前是拒绝并提示"必须以 / 开头"——真机里用户两次把命令当自由语输入
            # 被吞掉，"无法修改"的体感由此而来）。
            self._chat(line)
            return False
        # 拆出首词（命令名，可含连字符/下划线），其余整串作 args 保留。
        # 容忍 "/" 后空白（如 "/ seed"）；纯 "/"（或仅斜杠+空白）→ 空命令名 → 提示而非崩溃。
        m = re.match(r"/\s*(\S+)\s*(.*)$", line, re.S)
        if not m:
            self.io.output("[!] 空命令名——/help 查看。")
            return False
        name, args = m.group(1), m.group(2)
        table = {
            "projects": self.cmd_projects,
            "ls": self.cmd_projects,
            "new": self.cmd_new,
            "open": self.cmd_open,
            "show": self.cmd_show,
            "seed": self.cmd_seed,
            "build": self.cmd_build,
            "resume": self.cmd_resume,
            "conflicts": self.cmd_conflicts,
            "resolve": self.cmd_resolve,
            "shell": self.cmd_shell,
            "roll": self.cmd_roll,
            "roll-window": self.cmd_roll_window,
            "ingest": self.cmd_ingest,
            "craft": self.cmd_craft,
            "covenant": self.cmd_covenant,
            "lines-replay": self.cmd_lines_replay,
            "fr-review": self.cmd_fr_review,
            "fr-approve": self.cmd_fr_approve,
            "fr-approve-all": self.cmd_fr_approve_all,
            "fr-revise": self.cmd_fr_revise,
            "fr-switches": self.cmd_fr_switches,
            "chapter": self.cmd_chapter,
            "run": self.cmd_run,
            "status": self.cmd_run_status,
            "draft": self.cmd_draft,
            "review": self.cmd_review,
            "export": self.cmd_export,
            "stats": self.cmd_stats,
            "validate": self.cmd_validate_top,
            "forge-validate": self.cmd_contract,
            "feedback": self.cmd_feedback,
            "grant": self.cmd_grant,
            "characters-enrich": self.cmd_chars_enrich,
            "enrich-pending": self.cmd_enrich_pending,
            "settings-pending": self.cmd_settings_pending,
            "help": self.cmd_help,
            "agent": self.cmd_agent,
            "exit": self.cmd_exit,
            "quit": self.cmd_exit,
            "q": self.cmd_exit,
        }
        if name in table:
            return bool(table[name](args))
        self.io.output(f"[!] 未知命令 /{name}——/help 查看。")
        return False

    def run(self) -> ConsoleState:
        self.io.output("Novelist 控制台。`/help` 看全部命令；`/exit` 退出。")
        while True:
            prompt = f"novelist({self.state.project_id or 'no-project'})> "
            line = self.io.input(prompt)
            if line is None:
                break
            # U3 兜底（2026-09-15）：单个命令的未捕获异常不得终止会话（抛栈即丢状态）。
            # run_cli 那层已让 click 异常进 r.exception，这里是第二道网——覆盖 handler
            # 自身的错误（如 cmd_new 的解析、cmd_shell 的加载）。
            try:
                should_exit = self.dispatch(line)
            except (KeyboardInterrupt, EOFError):
                self.io.output("")
                break
            except Exception as e:  # noqa: BLE001 - REPL 必须活着
                self.io.output(f"[error] {type(e).__name__}: {e}")
                should_exit = False
            if should_exit:
                break
            self.state.commands_run += 1
        self.io.output("已退出控制台。")
        return self.state


# 接收 `--provider` 透传的命令（首 token 判定）。
# 不含 run/status/craft/show/covenant/validate/rollback/snapshots/grant/draft/
# export/stats/validate 等（这些无 --provider option，注入会报未知 option）。
_PROVIDER_CMDS = {
    "forge:seed", "forge:build", "forge:resume", "forge:shell",
    "forge:ingest", "forge:roll", "forge:roll-window",
    "chapter", "review", "feedback",
    "characters-enrich",
}


def _compose_argv(s: Console, argv: list[str]) -> list[str]:
    """给支持 provider 的命令统一追加 provider 透传参数。

    仅 `_PROVIDER_CMDS` 集合内的命令（按首 token 判定）才注入，避免 `init`、
    `run`/`status` 等无这些 option 的命令因多余 flag 报错。
    api_key/api_base/model 有值则一并带。
    """
    if not argv:
        return argv
    head = argv[0]
    if head == "forge" and len(argv) > 1:
        key = f"forge:{argv[1]}"
    else:
        key = head
    if key not in _PROVIDER_CMDS:
        return argv
    if any(a == "--provider" for a in argv):
        return argv
    out = list(argv)
    out += ["--provider", s.provider]
    if s.api_key:
        out += ["--api-key", s.api_key]
    if s.api_base:
        out += ["--api-base", s.api_base]
    if s.model:
        out += ["--model", s.model]
    return out


HELP_TEXT = """Novelist 控制台 —— 全命令列表（一律以 / 开头）

项目导航：
  /projects | /ls              列出全部项目（序号 标题 编号）
  /new <标题>                  新建项目并切入
  /open <id>                   选定当前项目

构建链（forge）：
  /seed "<一句话创意>"          一句话 → 提炼 + 建蓝图 + 构建（--smoke 只提炼）
  /show                        查看当前项目进度（已填设定/缺口/extras）
  /shell                       进入常驻会话，谈设定缺口 / 补设想（/build 触发构建）
  /build                       蓝图已有时重跑构建
  /resume                      断点续跑（商讨/构建，幂等）
  /roll <卷号>                 滚动细纲（每卷）
  /roll-window <宽>            未来窗口滚动
  /ingest <源目录/文件>         已有稿子 → 蓝图+正文+记忆初始化
  /forge-validate              契约校验（forge validate，V1–V6）
  /craft                       列题材工艺卡（无 LLM）
  /covenant                    查看承诺账本（伏笔兑付/卷主线/核心人设）
  /conflicts                   列出待裁决结构冲突（第二条主线 / 伏笔近重复）
  /resolve <id> <choice>        裁决一条冲突（选项见 /conflicts 输出）
  /lines-replay <卷> <章>      细纲修订转正（人工改细纲后重放线索）

构建期审核（forge review 系列）：
  /fr-review [模块]             查看待审模块（无参列 pending，给模块看全文）
  /fr-approve <模块|all> [--remember]  审核通过（all=批量批准全部待审）
  /fr-approve-all <模块|all> [--off]   放行机制：批准待审并永久关审核；--off 恢复
  /fr-revise <模块> "<建议>"    按建议重生成模块并展示差异
  /fr-switches [模块 on|off]    查看/设置模块审核开关

正文/流水线（顶层）：
  /chapter <卷> <章>            串行写一章（圣经注入→生成→润色→回写）
  /run [--to <工序>]            跑流水线到指定工序；不传 --to 则跑到终态
  /status                       查询进度与统计
  /draft [卷-章]                查看单章草稿源清单（--text 连正文）
  /export                       导出发布包(markdown)
  /stats                        统计

审核/一致性（顶层）：
  /review                       对已写正文做一次性一致性审查（只读报告）
  /feedback "<修改意见>"         把设定修改意见拆成审批项
  /grant                        处理待决门禁审批（--approve/--deny）

角色/设定待办（顶层）：
  /characters-enrich            批量丰富群像人物卡
  /enrich-pending               处理角色丰富待办
  /settings-pending [--allow x] 查看/处理设定待决项
  /validate                     顶层欠约束一致性检查（≠ /forge-validate 的契约校验）

其他：
  /help                         此帮助
  /agent                        对话 agent 状态（本轮轮数/用量/成本）
  /exit | /quit | /q            退出

对话（M3ac，2026-09-19）：
  · 非 / 开头的输入 = 自然语对话——主编剧 agent 会自主调只读工具查证后回答，
    可写草稿；写受控路径/发布/删除仍需你当场批准或走闸门命令。
  · 对话历史落 <项目>/workspace/agent/session.jsonl，重开 console 自动续接。

提示：
  · 命令以 / 开头；自然语直接输入（不再需要前缀）。
  · 顶层正文一致性审查用 /review；构建期模块待审用 /fr-review（二者不同）。
  · 两个 validate 不同：/validate = 顶层欠约束检查；/forge-validate = forge 契约校验。
  · /shell 会话内命令同样以 / 开头（如 /show /build /review /approve /revise）。
  · /run /status /craft /show 等无 LLM，不注入 provider。
  · /seed /build /chapter 等调 LLM；provider 在**启动 novelist console 时**指定
    （--provider/--api-key/--api-base/--model，默认 deepseek），进 REPL 后不可切换。
  · 每次 LLM 调用会写原始调用日志 ./raw-calls/YYYY-MM-DD.jsonl（含完整 prompt）；
    启动时加 --no-calllog 或设 NOVELIST_CALLLOG=0 关闭。
"""


def run_console(
    *,
    io: FilterableIO | None = None,
    workspace_root: str | None = None,
    provider: str = "deepseek",
    api_key: str | None = None,
    api_base: str | None = None,
    model: str | None = None,
    smoke: bool = False,
) -> ConsoleState:
    c = Console(
        io=io or FilterableIO(),
        workspace_root=workspace_root,
        provider=provider,
        api_key=api_key,
        api_base=api_base,
        model=model,
        smoke=smoke,
    )
    return c.run()