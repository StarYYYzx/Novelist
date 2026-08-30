"""测出 qwen3.5-9b 在真实 system prompt 下的思考开销，定出可用生成预算。

思考 token 计入 max_tokens：预算 = 思考 + 正文。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from novelist.core.embedding import make_embedding  # noqa: E402
from novelist.core.context import build_chapter_context  # noqa: E402
from novelist.storage.workspace import Workspace  # noqa: E402

BASE = "http://127.0.0.1:1234/v1/chat/completions"
MODEL = "qwen/qwen3.5-9b"
PID = "proj-duanyu"


def call(system: str, user: str, max_tokens: int) -> dict:
    payload = {"model": MODEL, "max_tokens": max_tokens, "temperature": 0.7,
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    req = urllib.request.Request(BASE, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=1200) as r:
        d = json.loads(r.read().decode("utf-8"))
    m = d["choices"][0]["message"]
    det = (d.get("usage") or {}).get("completion_tokens_details") or {}
    return {
        "content": m.get("content") or "",
        "reasoning": len(m.get("reasoning_content") or ""),
        "reasoning_tokens": det.get("reasoning_tokens"),
        "finish": d["choices"][0].get("finish_reason"),
        "secs": round(time.time() - t0),
    }


def main() -> None:
    ws = Workspace(root=str(ROOT / "novel_workspace"))
    ctx = build_chapter_context(ws, PID, 1, 1, genre="男频修仙")
    print(f"system prompt: {len(ctx.system_prompt)} 字；user goal: {len(ctx.user_goal)} 字")
    print(f"出场人物: {[c.get('name') for c in ctx.cast]}")
    print()
    for n in (1000, 1600, 2200, 3000):
        r = call(ctx.system_prompt, ctx.user_goal, n)
        print(f"max_tokens={n:5d}  正文 {len(r['content']):5d} 字 | 思考 {r['reasoning']:5d} 字 "
              f"({r['reasoning_tokens']} tok) | finish={r['finish']} | {r['secs']}s")
        if r["content"]:
            print(f"              开头: {r['content'][:50]!r}")
            print(f"              结尾: ...{r['content'][-30:]!r}")


if __name__ == "__main__":
    main()
