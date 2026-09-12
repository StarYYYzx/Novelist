#!/usr/bin/env python3
"""把 `.ai/skills`（技能唯一源）桥接到各 AI agent 工具的原生技能目录，并挂上 git hook。

用法（在仓库根目录执行）：

    python scripts/ai_bootstrap.py            # 建立链接（默认；Windows 用目录联接，无需管理员）
    python scripts/ai_bootstrap.py --copy     # 复制而非链接（不支持联接时用；会有漂移风险）
    python scripts/ai_bootstrap.py --status   # 只报告现状，不改动任何东西

为什么需要它
------------
技能的**唯一源**在 `.ai/skills/`（工具中立、随 git 分发）。但各工具只认自己的目录：
WorkBuddy 读 `.workbuddy/skills/`，Claude Code 读 `.claude/skills/`。目录联接（junction）
让**一份源**在多个工具的原生目录下同时可见 —— 零复制、零漂移、无需管理员权限。

每个成员 clone 后跑一次即可；AGENTS.md 里写了"接手第一步跑它"，所以 AI agent 读到也会替你执行。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_REL = ".ai/skills"
TARGETS = (".workbuddy/skills", ".claude/skills")
HOOKS_REL = ".githooks"
# hook 用的解释器记到 .git/ 下（机器本地、永不入库），免得把本机路径写进提交物
HOOK_PY_REL = ".git/novelist-hook-python"

OK = "[OK]  "
WARN = "[WARN]"
FAIL = "[FAIL]"


def _is_linked_to(link: Path, target: Path) -> bool:
    """link 是否已是指向 target 的链接（junction / symlink 均可）。"""
    if not link.exists() and not link.is_symlink():
        return False
    try:
        return Path(os.path.realpath(link)) == Path(os.path.realpath(target))
    except OSError:
        return False


def _make_link(link: Path, target: Path) -> tuple[bool, str]:
    if os.name == "nt":
        r = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
        )
        out = (r.stdout + r.stderr).decode("gbk", "replace").strip()
        return r.returncode == 0, out
    try:
        os.symlink(target, link, target_is_directory=True)
        return True, "symlink created"
    except OSError as e:
        return False, str(e)


def _visible_skills(d: Path) -> list[str]:
    if not d.exists():
        return []
    try:
        return sorted(x.name for x in d.iterdir() if (x / "SKILL.md").exists())
    except OSError:
        return []


def _setup_hook() -> None:
    hooks_dir = ROOT / HOOKS_REL
    hook = hooks_dir / "pre-commit"
    if not hook.exists():
        print(f"{WARN} 未找到 {HOOKS_REL}/pre-commit，跳过 git hook 配置")
        return

    r = subprocess.run(
        ["git", "config", "core.hooksPath", HOOKS_REL],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"{WARN} 设置 core.hooksPath 失败：{(r.stdout + r.stderr).strip()}")
    else:
        print(f"{OK} git hook 已挂载（core.hooksPath = {HOOKS_REL}）")

    if os.name != "nt":
        try:
            hook.chmod(0o755)
        except OSError:
            pass

    # 记录本机解释器，供 hook 使用（不入库）
    # 必须写成**正斜杠**形式：hook 由 Git 自带的 sh 执行，反斜杠路径在
    # `[ -x "$PY" ]` 判定下会失败，导致静默回退到 PATH 上碰到的任意 python。
    py_file = ROOT / HOOK_PY_REL
    try:
        py_file.parent.mkdir(parents=True, exist_ok=True)
        # newline="\n" 必须显式指定：Windows 上 write_text 默认写 CRLF，
        # hook 里 `read` 会读进尾随 \r，路径判定随即失败并静默回退 PATH。
        py_file.write_text(Path(sys.executable).as_posix(), encoding="utf-8", newline="\n")
        print(f"{OK} 记录本机解释器给 hook：{Path(sys.executable).as_posix()}")
    except OSError as e:
        print(f"{WARN} 无法记录解释器（{e}）；hook 将自行探测 python")


def main() -> int:
    ap = argparse.ArgumentParser(description="Novelist AI 规范/技能引导")
    ap.add_argument("--copy", action="store_true", help="复制而非建链接（有漂移风险）")
    ap.add_argument("--status", action="store_true", help="只报告现状")
    args = ap.parse_args()

    src = ROOT / SOURCE_REL
    print(f"技能源：{src}")
    if not src.is_dir():
        print(f"{FAIL} 技能源目录不存在，无法继续")
        return 1
    skills = _visible_skills(src)

    problems = 0
    for rel in TARGETS:
        dst = ROOT / rel
        if args.status:
            if _is_linked_to(dst, src):
                print(f"{OK} {rel:22} → 已链接到源（{len(_visible_skills(dst))} 个技能可见）")
            elif dst.is_dir():
                print(f"{WARN} {rel:22} 是真实目录（非链接），技能数 {len(_visible_skills(dst))}")
            else:
                print(f"{WARN} {rel:22} 不存在")
            continue

        if _is_linked_to(dst, src):
            print(f"{OK} {rel:22} 已就绪（{len(_visible_skills(dst))} 个技能可见）")
            continue

        if dst.is_dir() and not args.copy:
            print(f"{FAIL} {rel:22} 是真实目录，不是链接；为避免误删，本脚本不动它。")
            print("        请先确认该目录内容可弃（或手工合并），删除后重跑本脚本。")
            problems += 1
            continue

        dst.parent.mkdir(parents=True, exist_ok=True)
        if args.copy:
            shutil.copytree(src, dst, dirs_exist_ok=True)
            print(f"{OK} {rel:22} 已复制（{len(_visible_skills(dst))} 个技能）")
        else:
            good, msg = _make_link(dst, src)
            if good:
                print(f"{OK} {rel:22} 已建立目录联接（{len(_visible_skills(dst))} 个技能可见）")
            else:
                print(f"{FAIL} {rel:22} 建立联接失败：{msg}")
                print("        回退方案：python scripts/ai_bootstrap.py --copy")
                problems += 1

    if not args.status:
        _setup_hook()

    print()
    print(f"源内技能 {len(skills)} 个：{', '.join(skills) or '（空）'}")
    if problems:
        print(f"{FAIL} {problems} 个目标目录需要人工处理")
        return 1
    print(f"{OK} 引导完成。注意：新的技能作用域从**下一个会话**才开始生效。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
