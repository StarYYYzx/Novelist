"""准备 proj-yelan2：复制 proj-yelan 的 bible/outline，重置 worldstate，清空正文与记忆。

用途：同 bible 同细纲、不同模型（3080ti）重跑 ch1-10 的干净对比项目。
用法：python prepare_yelan2.py
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
DST = ROOT / "novel_workspace" / "proj-yelan2"


def main() -> None:
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
    proj["id"] = "proj-yelan2"
    (DST / "project.json").write_text(json.dumps(proj, ensure_ascii=False, indent=2), encoding="utf-8")

    # worldstate 初始态：从 bible 合成（ADR-019 时间轴 now=0 + 人物卡固有物品）
    ws = Workspace(root=str(ROOT / "novel_workspace"))
    init_from_bible(ws, "proj-yelan2")
    state = json.loads((DST / "bible" / "worldstate.json").read_text(encoding="utf-8"))
    chars = len(state.get("characters", {}))
    print(f"proj-yelan2 就绪：{len(list((DST/'bible').iterdir()))} bible 文件,"
          f" {len(list((DST/'outline'/'chapters').iterdir()))} 细纲, worldstate {chars} 人,"
          f" time.now={state.get('time', {}).get('now')}")


if __name__ == "__main__":
    main()
