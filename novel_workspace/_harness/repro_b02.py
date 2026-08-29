"""复现 B-02：不注入圣经时，CLI 直出会产生什么。

对照实验，用于让测试报告中的断言可被复核。在临时工作区里跑一次
`novelist chapter --provider lmstudio`（CLI 原样，不做任何 prompt 注入），
然后检查：主角性别是否漂移、是否凭空造人、章节是否被截断。

与 `_harness/run_novel.py` 的唯一差别就是**不注入圣经**（那是系统本身不给的）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
TMP = ROOT / "novel_workspace" / "_repro_b02"
PID = "proj-repro"

bible_names = ["苏晚", "铁无涯", "裴无忌", "阿岐", "赵虎"]


def run(cmd: list[str]) -> str:
    import os

    env = dict(os.environ)
    env.update({"PYTHONPATH": str(SRC), "PYTHONIOENCODING": "utf-8"})
    r = subprocess.run(
        [sys.executable, "-m", "novelist.cli", *cmd],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )
    return (r.stdout or "") + (r.stderr or "")


def main() -> None:
    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True)

    print("=== 1) 建最小项目（只有细纲 + 人物卡，人物卡**不含** gender）===")
    print(run(["init", str(TMP), "--title", "对照实验"]).strip())

    proj = next(p for p in TMP.iterdir() if p.is_dir())
    (proj / "bible" / "characters.json").write_text(
        json.dumps([{"id": "char:suwan", "name": "苏晚", "status": "active", "species": "human",
                     "core_traits": ["隐忍"], "power": {"level": "炼气三层", "faction": "青云宗"}}],
                   ensure_ascii=False),
        encoding="utf-8",
    )
    gist = proj / "outline" / "chapters" / "1-1.md"
    gist.parent.mkdir(parents=True, exist_ok=True)
    gist.write_text(
        "---\nid: ch:1:1\nvol: 1\nch: 1\ntitle: 弃徒\npov: 苏晚\n"
        "key_events: [苏晚被逐出内门, 拾得断玉佩]\n---\n\n"
        "## 细纲要点\n- 苏晚三年一考末位，被铁无涯当众摘去玉牌。\n"
        "- 下山途中踢到半枚焦黑玉佩，触之发烫。\n",
        # 注意：细纲里**不写**苏晚的性别——与首次真实运行条件一致，
        # 用于验证「人物卡无 gender 字段 + 圣经不注入 → LLM 自行漂移性别」。
        encoding="utf-8",
    )

    print("\n=== 2) 原样跑 CLI chapter（lmstudio，CLI 内硬编码 400 tokens）===")
    out = run(["chapter", str(TMP), "--provider", "lmstudio", "--vol", "1", "--ch", "1"])
    print(out.strip())

    draft = proj / "drafts" / "chapters" / "1-1.md"
    if not draft.exists():
        print("!! 未生成草稿")
        return
    text = draft.read_text(encoding="utf-8")

    print("\n=== 3) 检查结果 ===")
    she = re.findall(r"苏晚.{0,12}她", text)
    print(f"  a) 性别漂移（苏晚…她）：{len(she)} 处" + (f"  例：{she[0][:24]}" if she else "  ——未漂移"))

    invented = [m for m in re.findall(r"[一-龥]{2,3}(?=长老|师兄|师姐|师弟|宗主|护法|老祖)", text)
                if m not in bible_names]
    print(f"  b) 凭空造人：{sorted(set(invented)) or '无'}")

    print(f"  c) 章节字数：{len(text)}，末字：'{text.strip()[-1]}'"
          f"（{'截断' if text.strip()[-1] not in '。！？」）…”' else '完整'}）")
    print(f"  d) 细纲核心事件「玉佩」是否写进正文：{'是' if '玉佩' in text else '否'}")

    print("\n=== 4) 系统自身的一致性引擎怎么说 ===")
    sys.path.insert(0, str(SRC))
    from novelist.consistency import run_consistency
    from novelist.storage.workspace import Workspace

    alerts = run_consistency(Workspace(root=str(TMP)), next(p.name for p in TMP.iterdir() if p.is_dir()))
    print(f"  告警数：{len(alerts)}" + ("（对上述三类问题全部无感）" if not alerts else ""))


if __name__ == "__main__":
    main()
