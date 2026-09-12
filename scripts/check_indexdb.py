"""检查某个项目的 `.index.db`（SQLite 辅助索引）：表结构、行数、可选关键词命中。

原为仓库根目录下的一次性调试脚本，硬编码本机绝对路径；现移入 scripts/ 并参数化。

用法::

    python scripts/check_indexdb.py [项目目录] [--kw 关键词]

    # 不给项目目录时，取 novel_workspace 下最近修改的一个项目
    python scripts/check_indexdb.py --kw 杨铭
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

_TEXT_HINTS = ("text", "content", "summary", "body", "draft")


def _latest_project(root: Path) -> Path | None:
    cands = [p for p in root.iterdir() if p.is_dir() and (p / ".index.db").exists()]
    if not cands:
        return None
    return max(cands, key=lambda p: (p / ".index.db").stat().st_mtime)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="检查项目 .index.db 的表结构与内容")
    ap.add_argument("project", nargs="?", help="项目目录（省略则取 novel_workspace 下最近的一个）")
    ap.add_argument("--kw", default="", help="可选：在疑似正文列里搜索该关键词")
    ap.add_argument("--root", default="novel_workspace", help="项目根目录（默认 novel_workspace）")
    args = ap.parse_args(argv)

    if args.project:
        pj = Path(args.project)
    else:
        pj = _latest_project(Path(args.root))
        if pj is None:
            print(f"未在 {args.root} 下找到任何含 .index.db 的项目", file=sys.stderr)
            return 1
        print(f"# 使用最近修改的项目：{pj}")

    db_path = pj / ".index.db"
    if not db_path.exists():
        print(f"找不到 {db_path}", file=sys.stderr)
        return 1

    db = sqlite3.connect(str(db_path))
    tables = [r[0] for r in db.execute("select name from sqlite_master where type='table'")]
    print("tables:", tables)
    for t in tables:
        try:
            n = db.execute(f"select count(*) from {t}").fetchone()[0]
            cols = [c[1] for c in db.execute(f"pragma table_info({t})")]
            print(f"  {t}: {n} rows; cols={cols}")
        except sqlite3.Error as e:  # noqa: PERF203  单表失败不应中断其余表
            print("  ", t, e)

    if args.kw:
        print(f"--- 关键词命中: {args.kw} ---")
        for t in tables:
            cols = [c[1] for c in db.execute(f"pragma table_info({t})")]
            for c in [c for c in cols if any(k in c for k in _TEXT_HINTS)]:
                try:
                    rows = db.execute(
                        f"select {c} from {t} where {c} like ? limit 3", (f"%{args.kw}%",)
                    ).fetchall()
                except sqlite3.Error:
                    continue
                if rows:
                    print(f"  [{t}.{c}] {len(rows)} 命中; 样本长度={len(rows[0][0] or '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
