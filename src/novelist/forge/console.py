"""项目交互中枢（`novelist console`，2026-09-10）。

一行指令启动 → 进入常驻 REPL，集项目导航与完整新建/编辑/构建于一体。

**命令面（全部接入 CLI 指令，2026-09-10）**
- 项目导航：`projects` / `ls` / `new <标题>` / `open <id>`
- 构建链（forge）：`seed` `build` `resume` `shell` `roll` `roll-window`
  `ingest` `validate` `show` `craft` `covenant` `lines-replay`
  `rollback` `snapshots` `fr-review` `fr-approve` `fr-revise` `fr-switches`
- 正文/流水线（顶层）：`chapter` `run` `status` `draft` `export`
- 审核/一致性（顶层）：`review`（正文一致性）`feedback` `grant`
- 统计与设定：`stats` `validate`（顶层欠约束检查）
- 纪要：`characters-enrich` `enrich-pending` `settings-pending` `settings-allow`
- 帮助/退出：`help` `exit` / `quit` / `q`

**参数定位纪律（D7，与 cli.py 逐字对齐，勿再混用）**
- 位置参数 `[DIRECTORY]`：build/roll/roll-window/resume/shell/show/craft/covenant/
  validate/rollback/snapshots/ingest(SOURCE 后)/lines-replay，及顶层 run/status/
  review/feedback/grant/export/stats/draft/characters-enrich/enrich-pending/
  settings-pending/chapter(--vol/--ch)
- `--dir` option：seed、fr-review/fr-approve/fr-revise/fr-switches
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
from dataclasses import dataclass, field

from click.testing import CliRunner

from ..cli import cli as cli_group
from ..storage.workspace import Workspace


@dataclass
class ConsoleState:
    ok: bool = True
    project_id: str | None = None
    commands_run: int = 0
    logged: list[str] = field(default_factory=list)

    def log(self, line: str) -> None:
        self.logged.append(line)


# —— REPL 输入原语：薄封装，便于测试注入 ——

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
        # 真实终端（tty 且非脚本注入）直接打印——否则 REPL 全程静默
        if self._tty and not self._lines:
            print(text, flush=True)

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

    # —— 工具 ——

    def _proj_dir(self, pid: str) -> str:
        return (self._ws.project_dir(pid)).as_posix()

    def _project_loc(self) -> str | None:
        return self._proj_dir(self.state.project_id) if self.state.project_id else None

    def _need_project(self) -> bool:
        if self.state.project_id is None:
            self.io.output("[!] 未选定项目：`open <id>` 或 `new <标题>` 先建/选一个。")
            return False
        return True

    def run_cli(self, argv: list[str]):
        """复调 click CLI，输出转发到 io（自动缝合 provider 透传参数）。

        返回 click TestResult，便于调用方解析（如 `new` 从 `created project <id>`
        精确拿到新建项目 id，而非猜列表末位）。
        """
        full = _compose_argv(self, argv)
        r = CliRunner().invoke(cli_group, full, catch_exceptions=False)
        if r.output:
            lines = [ln for ln in r.output.splitlines() if ln.strip()]
            self.io.output("\n".join(lines))
        if r.exception:
            self.io.output(f"[error] {getattr(r.exception, 'message', r.exception)}")
        return r

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
            self.io.output("（尚无项目——`new <标题>` 新建。）")
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
            self.io.output("[!] 未能解析新建项目 id——用 `projects` 查看")

    def cmd_open(self, args: str) -> None:
        pid = args.strip()
        if not pid:
            self.io.output("[!] 用法：open <id>")
            return
        if pid not in self._ws.list_projects():
            self.io.output(f"[!] 未找到项目 {pid!r}（`projects` 查看）")
            return
        self.state.project_id = pid
        title, stage = self._meta(pid)
        self.io.output(f"=> {pid}（{title or ''}，stage {stage or '—'}）")

    def cmd_show(self, args: str) -> None:
        self._proj_run("show")

    def cmd_seed(self, args: str) -> None:
        brief = args.strip()
        if not brief:
            self.io.output("[!] 用法：seed \"一句话创意\"")
            return
        if not self._need_project():
            return
        argv = ["forge", "seed", brief, "--dir", self._project_loc(), "--mode", "interactive"]
        if self.smoke:
            argv.append("--smoke")
        self.run_cli(argv)

    def cmd_build(self, args: str) -> None:
        self._proj_run("build")

    def cmd_resume(self, args: str) -> None:
        self._proj_run("resume")

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
            self.io.output("[!] 用法：roll <卷号>")
            return
        self._proj_run("roll", [vol])

    def cmd_roll_window(self, args: str) -> None:
        vol = args.strip().split()[0] if args.strip() else None
        if not vol or not vol.isdigit():
            self.io.output("[!] 用法：roll-window <宽>")
            return
        self._proj_run("roll-window", [vol])

    def cmd_ingest(self, args: str) -> None:
        parts = args.strip().split()
        if not parts:
            self.io.output("[!] 用法：ingest <源目录/文件>")
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
            self.io.output("[!] 用法：lines-replay <卷> <章>")
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
        module = args.strip()
        if not module:
            self.io.output("[!] 用法：fr-approve <模块> [--remember]")
            return
        if not self._need_project():
            return
        self.run_cli(["forge", "approve", module, "--dir", self._project_loc()])

    def cmd_fr_revise(self, args: str) -> None:
        module = args.strip()
        if not module:
            self.io.output('[!] 用法：fr-revise <模块> "<修改建议>"')
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
            self.io.output("[!] 用法：chapter <卷> <章>")
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

    def cmd_exit(self, args: str) -> bool:
        return True

    def dispatch(self, line: str) -> bool:
        """分发一行输入；返回 True 表示应退出 REPL。"""
        line = line.strip()
        if not line:
            return False
        # 拆出首词（命令名，可含连字符/下划线），其余整串作 args 保留
        m = re.match(r"(\S+)\s*(.*)$", line, re.S)
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
            "shell": self.cmd_shell,
            "roll": self.cmd_roll,
            "roll-window": self.cmd_roll_window,
            "ingest": self.cmd_ingest,
            "craft": self.cmd_craft,
            "covenant": self.cmd_covenant,
            "lines-replay": self.cmd_lines_replay,
            "fr-review": self.cmd_fr_review,
            "fr-approve": self.cmd_fr_approve,
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
            "feedback": self.cmd_feedback,
            "grant": self.cmd_grant,
            "characters-enrich": self.cmd_chars_enrich,
            "enrich-pending": self.cmd_enrich_pending,
            "settings-pending": self.cmd_settings_pending,
            "help": self.cmd_help,
            "exit": self.cmd_exit,
            "quit": self.cmd_exit,
            "q": self.cmd_exit,
        }
        if name in table:
            return bool(table[name](args))
        self.io.output(f"[!] 未知命令 {name!r}——`help` 查看。")
        return False

    def run(self) -> ConsoleState:
        self.io.output("Novelist 控制台。`help` 看全部命令；`q` 退出。")
        while True:
            prompt = f"novelist({self.state.project_id or 'no-project'})> "
            line = self.io.input(prompt)
            if line is None:
                break
            if self.dispatch(line):
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


HELP_TEXT = """Novelist 控制台 —— 全命令列表

项目导航：
  projects / ls                列出全部项目（序号 标题 编号）
  new <标题>                   新建项目并切入
  open <id>                    选定当前项目

构建链（forge）：
  seed "<一句话创意>"           一句话 → 提炼 + 建蓝图 + 构建（--smoke 只提炼）
  show                         查看当前项目进度（已填设定/缺口/extras）
  shell                        进入常驻会话，谈设定缺口 / 补设想（/build 触发构建）
  build                        蓝图已有时重跑构建
  resume                       断点续跑（商讨/构建，幂等）
  roll <卷号>                  滚动细纲（每卷）
  roll-window <宽>             未来窗口滚动
  ingest <源目录/文件>          已有稿子 → 蓝图+正文+记忆初始化
  validate                     契约校验（forge validate）
  craft                        列题材工艺卡（无 LLM）
  covenant                     查看承诺账本（伏笔兑付/卷主线/核心人设）
  lines-replay <卷> <章>       细纲修订转正（人工改细纲后重放线索）

构建期审核（forge review 系列）：
  fr-review [模块]             查看待审模块（无参列 pending，给模块看全文）
  fr-approve <模块>            审核通过该模块
  fr-revise <模块> "<建议>"    按建议重生成模块并展示差异
  fr-switches [模块 on|off]    查看/设置模块审核开关

正文/流水线（顶层）：
  chapter <卷> <章>            串行写一章（圣经注入→生成→润色→回写）
  run [工序或--to 工序]         跑流水线到指定工序；审查阶段做一致性检查
  status                       查询进度与统计
  draft [卷-章]                查看单章草稿源清单（--text 连正文）
  export                       导出发布包(markdown)
  stats                        统计

审核/一致性（顶层）：
  review                       对已写正文做一次性一致性审查（只读报告）
  feedback "<修改意见>"         把设定修改意见拆成审批项
  grant                        处理待决门禁审批（--approve/--deny）

角色/设定待办（顶层）：
  characters-enrich            批量丰富群像人物卡
  enrich-pending               处理角色丰富待办
  settings-pending [--allow x] 查看/处理设定待决项
  validate                     顶层欠约束一致性检查

其他：
  help                         此帮助
  exit / quit / q              退出

提示：
  · 顶层正文一致性审查用 `review`；构建期模块待审用 `fr-review`（二者不同）。
  · `run`/`status`/`craft`/`show` 等无 LLM，不注入 provider。
  · `seed`/`build`/`chapter` 等调 LLM，provider 在启动时指定（默认 deepseek）。
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