"""准备 proj-yelan3：复制 proj-yelan 的 bible/outline，重置 worldstate，清空正文与记忆。

用途：同 bible 同细纲、不同模型（服务器 C FreeToken / Qwen3.6-35B-A3B-FP8）重跑的干净项目，
与 proj-yelan2（v6 / Qwen3.5-9B @3080ti）构成同起点对比。
用法：python prepare_yelan3.py
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from novelist.core.worldstate import init_from_bible  # noqa: E402
from novelist.storage.workspace import Workspace  # noqa: E402

SRC = ROOT / "novel_workspace" / "proj-yelan"
DST = ROOT / "novel_workspace" / "proj-yelan3"


def main():
    if DST.exists():
        raise SystemExit(f"目标已存在：{DST}（先确认无价值再手动清理）")
    DST.mkdir()

    # bible：复制全部，排除 worldstate.json（运行态，重新初始化）
    shutil.copytree(SRC / "bible", DST / "bible")
    (DST / "bible" / "worldstate.json").unlink()

    # outline：细纲原样
    shutil.copytree(SRC / "outline", DST / "outline")

    # 空目录
    for name in ("chapters", "drafts", "memory", "logs", "takes", "reports", "workspace"):
        (DST / name).mkdir()

    # project.json：换 id
    proj = json.loads((SRC / "project.json").read_text(encoding="utf-8"))
    proj["id"] = "proj-yelan3"
    (DST / "project.json").write_text(json.dumps(proj, ensure_ascii=False, indent=2), encoding="utf-8")

    # worldstate 初始态：从 bible 合成（ADR-019 时间轴 now=0 + 人物卡固有物品）
    ws = Workspace(root=str(ROOT / "novel_workspace"))
    init_from_bible(ws, "proj-yelan3")
    state = json.loads((DST / "bible" / "worldstate.json").read_text(encoding="utf-8"))
    chars = len(state.get("characters", {}))
    print(f"proj-yelan3 就绪：{len(list((DST/'bible').iterdir()))} bible 文件,"
          f" {len(list((DST/'outline'/'chapters').iterdir()))} 细纲, worldstate {chars} 人,"
          f" time.now={state.get('time', {}).get('now')}")


if __name__ == "__main__":
    main()
