"""检查各章是否被截断（末行非句末标点），供决定是否重生成。

系统本身不做此检查：`produce_chapter` 只判断 `len(final) >= direct_words_floor`（默认 20 字符），
被拦腰截断的章节同样被记为成功。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PID = "proj-duanyu"
d = Path(__file__).resolve().parents[1] / PID / "drafts" / "chapters"
END = "。！？」）】…》”’\"'"

meta = re.compile(r"第\s*\d+\s*章")
leak = re.compile(r"第[一二三四五六七八九十\d]+章|本章|上一章|下一章|细纲|大纲")

bad = []
for f in sorted(d.glob("*.md"), key=lambda p: int(p.stem.split("-")[1])):
    text = f.read_text(encoding="utf-8").strip()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    last = lines[-1].strip() if lines else ""
    ok_end = bool(last) and last[-1] in END
    leaks = sorted(set(meta.findall(text))) + sorted(set(leak.findall(text)))
    flag = "" if ok_end else "  <-- 截断"
    print(f"{f.stem:6s} 字符={len(text):5d}  末字='{last[-1] if last else ''}'{flag}"
          + (f"  元叙事泄漏={leaks}" if leaks else ""))
    if not ok_end:
        bad.append(int(f.stem.split("-")[1]))

print()
print("需重生成的章节:", bad if bad else "无")
print("命令行：python novel_workspace/_harness/regen.py " + " ".join(str(x) for x in bad))
