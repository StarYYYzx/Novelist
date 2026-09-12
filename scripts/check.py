#!/usr/bin/env python3
"""Novelist 仓库门禁 —— 人与 AI agent 共用同一条命令。

用法（在仓库根目录执行）：

    python scripts/check.py --quick          # 层A：ruff + 密钥扫描 + 卫生检查（<3s，pre-commit 挂这个）
    python scripts/check.py --all            # 层A + pytest 全量 + 文档基线校验（约 90s，提交前 / CI）
    python scripts/check.py --docs-baseline  # 只跑文档基线校验
    python scripts/check.py --list           # 列出全部检查项

设计约束：
- 除 ruff / pytest 外不依赖第三方包，任何环境都能跑。
- pytest 子进程**强制注入**沙箱环境变量，避免"忘记设置 → 偶发假失败"这类最难查的问题。
- 检查项**宁可少而稳**：误报率高或语义模糊的（如"零引用死代码扫描"）刻意不做，
  见 AGENTS.md §门禁。

退出码：0 = 全部通过；1 = 有失败项。
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SELF = Path(__file__).resolve()

# CI（GitHub Actions Windows runner）stdout 默认 cp1252，门禁输出含大量中文，
# 第一行 print 就会 UnicodeEncodeError。统一强制 UTF-8（Python 3.7+），
# errors="replace" 保证任何异常字节都不再崩掉门禁本身。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------- 输出工具

_OK = "[OK]  "
_FAIL = "[FAIL]"
_SKIP = "[SKIP]"
_WARN = "[WARN]"

_results: list[tuple[str, bool]] = []


def _say(mark: str, msg: str) -> None:
    print(f"{mark} {msg}")


def _head(title: str) -> None:
    print()
    print(f"--- {title} " + "-" * max(0, 60 - len(title)))


# ---------------------------------------------------------------- 子进程


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **kw,
    )


def _find_ruff() -> str | None:
    """按 RUFF_BIN → PATH → 本机已知安装位置 的顺序解析 ruff。"""
    cands = [
        os.environ.get("RUFF_BIN"),
        shutil.which("ruff"),
        r"E:\python\ana\Scripts\ruff.EXE",
        str(Path.home() / "AppData" / "Roaming" / "Python" / "Scripts" / "ruff.exe"),
    ]
    for c in cands:
        if c and (shutil.which(c) or Path(c).exists()):
            return c
    return None


# ---------------------------------------------------------------- G1 ruff


def gate_ruff() -> bool:
    _head("G1 · ruff 静态检查（src / tests / scripts）")
    ruff = _find_ruff()
    if ruff is None:
        _say(_SKIP, "未找到 ruff（装法：pip install ruff==0.12.0；或设 RUFF_BIN）")
        return True
    r = _run([ruff, "check", "src", "tests", "scripts"])
    out = (r.stdout + r.stderr).strip()
    if r.returncode == 0:
        _say(_OK, f"ruff 全绿（{ruff}）")
        return True
    _say(_FAIL, "ruff 有告警：")
    print(out)
    return False


# ---------------------------------------------------------------- G2 密钥扫描

_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"), "疑似 API key（sk- 前缀）"),
    (re.compile(r"\bghp_[A-Za-z0-9]{30,}"), "疑似 GitHub PAT"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "疑似 AWS Access Key"),
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"), "私钥块"),
]

# 这些文件名一旦被 git 跟踪即视为事故
_SENSITIVE_BASENAMES = (".env", ".deepseek_key", "autodl_ssh.txt")

_SCAN_EXT = {
    ".py", ".md", ".txt", ".json", ".jsonl", ".toml", ".yml", ".yaml",
    ".cfg", ".ini", ".sh", ".ps1", ".csv", ".example",
}

_ALLOWLIST_MARK = "allowlist secret"

_MAX_SCAN_BYTES = 1 << 20


def _tracked_files() -> list[str]:
    r = _run(["git", "ls-files", "-z"])
    return [p for p in r.stdout.split("\0") if p]


def gate_secrets() -> bool:
    _head("G2 · 密钥与敏感文件扫描")
    ok = True

    tracked = _tracked_files()
    if not tracked:
        _say(_SKIP, "git ls-files 无输出（不是 git 仓库？）")
        return True

    for p in tracked:
        if Path(p).name in _SENSITIVE_BASENAMES:
            _say(_FAIL, f"敏感文件被 git 跟踪：{p}")
            ok = False

    hits: list[str] = []
    for p in tracked:
        path = ROOT / p
        if p.replace("\\", "/") == "scripts/check.py":
            continue
        if path.suffix.lower() not in _SCAN_EXT:
            continue
        try:
            if path.stat().st_size > _MAX_SCAN_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if _ALLOWLIST_MARK in line:
                continue
            for pat, label in _SECRET_PATTERNS:
                if pat.search(line):
                    hits.append(f"{p}:{lineno}  {label}")
                    break

    if hits:
        _say(_FAIL, f"发现 {len(hits)} 处疑似凭据：")
        for h in hits[:30]:
            print("        " + h)
        if len(hits) > 30:
            print(f"        ...（另有 {len(hits) - 30} 处）")
        print(f'        误报可在该行加 "{_ALLOWLIST_MARK}" 豁免')
        ok = False

    if ok:
        _say(_OK, f"已扫描 {len(tracked)} 个被跟踪文件，未发现凭据或敏感文件")
    return ok


# ---------------------------------------------------------------- G3 卫生检查

_REQUIRED_GITIGNORE = (
    ".env",
    ".workbuddy/",
    ".claude/",
    "tests/test_noval/",
    "*.index.db",
    ".pytest_cache/",
    "autodl_ssh.txt",
    "/_*.py",  # 根目录临时脚本：靠这条挡住 git add -A（G3 另有 on-disk 提醒）
)

_ROOT_LEFTOVER_RE = re.compile(r"^_(?!_)[^/]*\.(?:py|txt|md|json|log|lock)$")


def gate_hygiene() -> bool:
    _head("G3 · 仓库卫生检查")
    ok = True

    # (a) 历史上已入库的根目录临时文件 —— 说明 .gitignore 是后补的，应当清掉
    tracked_junk = [
        p
        for p in _tracked_files()
        if "/" not in p.replace("\\", "/") and _ROOT_LEFTOVER_RE.match(p)
    ]
    if tracked_junk:
        _say(_FAIL, f"根目录临时文件已被 git 跟踪 {len(tracked_junk)} 个（应 git rm --cached）：")
        for p in tracked_junk:
            print("        " + p)
        ok = False

    # (b) on-disk 残留 —— 已被 .gitignore 挡住提交，故只提醒不阻塞
    on_disk = sorted(
        p.name
        for p in ROOT.iterdir()
        if p.is_file() and _ROOT_LEFTOVER_RE.match(p.name)
    )
    if on_disk:
        _say(_WARN, f"根目录有 {len(on_disk)} 个 `_*` 临时文件（提交不进去，记得清理）："
                    f"{', '.join(on_disk[:8])}")

    gi = ROOT / ".gitignore"
    text = gi.read_text(encoding="utf-8") if gi.exists() else ""
    missing = [e for e in _REQUIRED_GITIGNORE if e not in text]
    if missing:
        _say(_FAIL, f".gitignore 缺少必需条目：{missing}")
        print("        （这些条目是「提交不进去」的唯一保障，删掉即失去保护）")
        ok = False

    if _run(["git", "ls-files", "tests/test_noval"]).stdout.strip():
        _say(_FAIL, "tests/test_noval/ 被 git 跟踪（用户手写稿，只读不提交）")
        ok = False

    r = _run(["git", "config", "--get", "core.hooksPath"])
    if r.stdout.strip() != ".githooks":
        _say(_WARN, "未挂 git hook：跑 python scripts/ai_bootstrap.py（不阻塞，但本地门禁缺失）")

    if ok:
        _say(_OK, "无临时文件入库；.gitignore 关键条目齐备；test_noval 未被跟踪")
    return ok


# ---------------------------------------------------------------- G4 pytest


def gate_pytest() -> bool:
    _head("G4 · pytest 全量回归")
    env = dict(os.environ)
    env["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"
    env["CODEBUDDY_SAFE_DELETE_BULK_THRESHOLD"] = "100000"
    r = _run([sys.executable, "-m", "pytest", "tests/", "-q", "--tb=short"], env=env)
    lines = [ln for ln in (r.stdout + r.stderr).strip().splitlines() if ln.strip()]
    for ln in lines[-12:]:
        print("      " + ln)
    if r.returncode == 0:
        _say(_OK, "回归通过（已注入沙箱环境变量）")
        return True
    _say(_FAIL, f"回归失败（returncode={r.returncode}）")
    return False


# ---------------------------------------------------------------- G5 文档基线

_SRC = ROOT / "src"
_TESTS = ROOT / "tests"


def _iter_src_py():
    for p in _SRC.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        yield p


def _metrics() -> dict[str, object]:
    """重算 docs/11 基线表里那几项。指标**刻意少而稳**，加一项就要同步文档。"""
    files = sorted(_iter_src_py())
    line_counts = {p: len(p.read_text(encoding="utf-8").splitlines()) for p in files}
    src_lines = sum(line_counts.values())
    max_file = max(line_counts.items(), key=lambda kv: kv[1]) if line_counts else (Path("?"), 0)

    func_total = 0
    big50 = 0
    big100 = 0
    bare_except = 0
    todo = 0
    fixme = 0
    for p in files:
        text = p.read_text(encoding="utf-8")
        todo += len(re.findall(r"\bTODO\b", text))
        fixme += len(re.findall(r"\bFIXME\b", text))
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func_total += 1
                span = (node.end_lineno or node.lineno) - node.lineno + 1
                if span > 50:
                    big50 += 1
                if span > 100:
                    big100 += 1
            elif isinstance(node, ast.ExceptHandler) and node.type is None:
                bare_except += 1

    abs_files = 0
    abs_calls = 0
    for p in files:
        if p.name == "workspace.py" and p.parent.name == "storage":
            continue
        n = len(re.findall(r"\._abs\(", p.read_text(encoding="utf-8")))
        if n:
            abs_files += 1
            abs_calls += n

    tests_files = len([p for p in _TESTS.rglob("*.py") if "__pycache__" not in p.parts])
    tests_lines = sum(
        len(p.read_text(encoding="utf-8").splitlines())
        for p in _TESTS.rglob("*.py")
        if "__pycache__" not in p.parts
    )

    env = dict(os.environ)
    env["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"
    env["CODEBUDDY_SAFE_DELETE_BULK_THRESHOLD"] = "100000"
    r = _run([sys.executable, "-m", "pytest", "tests/", "-q", "--collect-only"], env=env)
    m = re.search(r"(\d+)\s+tests?\s+collected", r.stdout + r.stderr)
    collected = int(m.group(1)) if m else -1

    ruff = _find_ruff()
    ruff_count = -1
    if ruff:
        rr = _run([ruff, "check", "src", "tests", "scripts", "--output-format=json"])
        try:
            ruff_count = len(json.loads(rr.stdout or "[]"))
        except json.JSONDecodeError:
            ruff_count = -1

    doc = ""
    for candidate in ("docs/11-coding-standard.md", "docs/11-编码规范.md"):
        if (ROOT / candidate).exists():
            doc = (ROOT / candidate).read_text(encoding="utf-8")
            break

    return {
        "src": (src_lines, len(files)),
        "max_file": (max_file[0].name, max_file[1]),
        "func_total": func_total,
        "gt50": big50,
        "gt100": big100,
        "abs": (abs_files, abs_calls),
        "tests": (tests_files, tests_lines, collected),
        "ruff": ruff_count,
        "bool_flags": (bare_except, todo, fixme),
        "doc_text": doc,
    }


_ROW_SPECS: list[tuple[str, str, int]] = [
    # 文档行标签, 指标键, 该单元格里要提取的数字个数
    ("src 规模", "src", 2),
    ("最大文件", "max_file", 1),
    ("函数总数", "func_total", 1),
    (">50 行函数", "gt50", 1),
    (">100 行函数", "gt100", 1),
    ("`ws._abs` 外部调用", "abs", 2),
    ("ruff 告警", "ruff", 1),
    ("测试", "tests", 3),
    ("裸 except / TODO / FIXME", "bool_flags", 3),
]


def _parse_doc_cells(doc: str) -> dict[str, tuple[list[int], str]]:
    """从 docs/11 基线表第三列（2026-09-12 实测列）抽数字。

    返回 {指标键: (数字列表, 该单元格原文)}。
    """
    out: dict[str, tuple[list[int], str]] = {}
    labels = {spec[0]: spec[1] for spec in _ROW_SPECS}
    for line in doc.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        key = labels.get(cells[0])
        if key is None or key in out:
            continue
        bold = re.search(r"\*\*(.+?)\*\*", cells[2])
        if bold:
            out[key] = ([int(x) for x in re.findall(r"\d+", bold.group(1))], bold.group(1))
    return out


def gate_docs_baseline() -> bool:
    _head("G5 · 文档基线校验（docs/11 附:体检数据基线）")
    m = _metrics()
    doc = str(m.pop("doc_text"))
    if not doc:
        _say(_SKIP, "未找到 docs/11，跳过")
        return True

    parsed = _parse_doc_cells(doc)
    failures: list[str] = []
    rows: list[tuple[str, str, str]] = []

    for row_label, key, n in _ROW_SPECS:
        if key not in parsed:
            failures.append(f"{row_label}: 文档表格里未解析到该行")
            continue
        doc_nums, raw = parsed[key]
        doc_nums = doc_nums[:n]
        val = m[key]
        actual_nums = list(val) if isinstance(val, tuple) else [val]
        if key == "max_file":
            name, lines = val  # type: ignore[misc]
            actual_nums = [lines]
            # 文档习惯写不带扩展名的模块名（如 "orchestrator 2610"），故按 stem 比对
            stem = Path(name).stem
            if stem not in raw:
                failures.append(f"最大文件: 文档写 {raw!r}，实际最大是 {stem}（{lines} 行）")
        actual_nums = actual_nums[:n]
        rows.append((row_label, str(doc_nums), str(actual_nums)))
        if doc_nums != actual_nums:
            failures.append(f"{row_label}: 文档 {doc_nums} ≠ 实际 {actual_nums}")

    print(f"      {'':2}{'指标':24}{'文档值':24}实测值")
    for label, d, a in rows:
        print(f"      {'  ' if d == a else '!!'}{label:24}{d:24}{a}")

    if failures:
        _say(_FAIL, f"{len(failures)} 项与文档不一致：")
        for f in failures:
            print("        " + f)
        print("        修法：更新 docs/11 附:体检数据基线的「2026-09-12（实测）」列")
        return False

    _say(_OK, f"{len(rows)} 项指标与 docs/11 基线表完全一致")
    return True


# ---------------------------------------------------------------- 编排

_GATES = {
    "ruff": ("G1 ruff 静态检查", gate_ruff),
    "secrets": ("G2 密钥与敏感文件扫描", gate_secrets),
    "hygiene": ("G3 仓库卫生检查", gate_hygiene),
    "pytest": ("G4 pytest 全量回归", gate_pytest),
    "docs": ("G5 文档基线校验", gate_docs_baseline),
}

_QUICK = ("ruff", "secrets", "hygiene")
_ALL = ("ruff", "secrets", "hygiene", "pytest", "docs")


def main() -> int:
    ap = argparse.ArgumentParser(description="Novelist 仓库门禁")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--quick", action="store_true", help="层A：ruff + 密钥 + 卫生（<3s）")
    g.add_argument("--all", action="store_true", help="全部检查（默认，约 90s）")
    g.add_argument("--docs-baseline", action="store_true", help="只跑文档基线校验")
    g.add_argument("--list", action="store_true", help="列出检查项")
    args = ap.parse_args()

    if args.list:
        for name in _ALL:
            layer = "A" if name in _QUICK else "B"
            print(f"  层{layer}  {name:10} {_GATES[name][0]}")
        return 0

    if args.docs_baseline:
        selected = ("docs",)
    elif args.quick:
        selected = _QUICK
    else:
        selected = _ALL

    print(f"Novelist 门禁 · {' → '.join(selected)}")
    print(f"仓库根：{ROOT}    解释器：{sys.executable}")

    for name in selected:
        label, fn = _GATES[name]
        try:
            good = fn()
        except Exception as e:  # noqa: BLE001 - 门禁自身出错要报出来而不是崩掉
            _say(_FAIL, f"{label} 执行异常：{type(e).__name__}: {e}")
            good = False
        _results.append((label, good))

    print()
    print("=" * 68)
    for label, good in _results:
        _say(_OK if good else _FAIL, label)
    failed = [label for label, good in _results if not good]
    if failed:
        print(f"\n门禁未通过：{len(failed)}/{len(_results)} 项失败")
        return 1
    print(f"\n门禁全部通过（{len(_results)} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
