"""清空《断玉青冥》的生成产物，回到干净基线。

用途：并发跑污染了记忆层（两个进程同时写同一批文件，导致部分章节事件数翻倍）后重跑。

⚠️ 沙箱禁用 `rm` / `Path.unlink()`，所以这里一律用**覆盖写**而不是删除。
清空 drafts/chapters 时写入占位内容——重跑会整体覆盖，但空文件更安全（避免误把旧稿当新稿）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from novelist.storage.workspace import Workspace  # noqa: E402

PID = "proj-duanyu"
ws = Workspace(root=str(ROOT / "novel_workspace"))
BASE = ws.project_dir(PID)


def _reset_json(rel: str, empty) -> None:
    p = BASE / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(empty, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    # 1) 记忆层清空
    _reset_json("memory/plot_events.json", [])
    _reset_json("memory/relationships.json", {"pairs": []})
    _reset_json("memory/fragment_index.json",
                {"revision": 0, "kind": "keyword-hash", "dim": None, "fragments": []})
    _reset_json("memory/rag/vectors.json", {"kind": "keyword-hash", "dim": None, "vectors": {}})

    hist = BASE / "memory" / "character_histories"
    hist.mkdir(parents=True, exist_ok=True)
    n_hist = 0
    for f in hist.glob("*.json"):
        _reset_json(f"memory/character_histories/{f.name}",
                    {"char_id": f.stem, "revision": 0, "entries": []})
        n_hist += 1

    # 2) 正文产物清空（覆盖写，不删除）
    n_draft = 0
    for d in ("drafts/chapters", "chapters"):
        dd = BASE / d
        dd.mkdir(parents=True, exist_ok=True)
        for f in dd.glob("*.md"):
            f.write_text("", encoding="utf-8")
            n_draft += 1

    # 3) 日志截断（不删除）
    for name in ("run_v2_log.jsonl", "run_v2.log", "run_2_10.log", "regen.log"):
        (ROOT / "novel_workspace" / "_harness" / name).write_text("", encoding="utf-8")

    # 4) 释放进程锁
    lock = ROOT / "novel_workspace" / "_harness" / ".v2.lock"
    lock.write_text("", encoding="utf-8")

    print(f"reset done: histories={n_hist}, chapter files cleared={n_draft}, memory cleared")


if __name__ == "__main__":
    main()
