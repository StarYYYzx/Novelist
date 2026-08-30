"""单点探测：真实 system prompt 下，多大的 max_tokens 才开始吐正文。

一次只跑一个预算值（GPU 显存吃紧，避免连跑把 9B 模型挤爆）。
用法：python probe_budget2.py <max_tokens>
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from novelist.core.context import build_chapter_context  # noqa: E402
from novelist.storage.workspace import Workspace  # noqa: E402

BASE = "http://127.0.0.1:1234/v1/chat/completions"
MODEL = "qwen/qwen3.5-9b"
PID = "proj-duanyu"

n = int(sys.argv[1]) if len(sys.argv) > 1 else 2500
ch = int(sys.argv[2]) if len(sys.argv) > 2 else 1

ws = Workspace(root=str(ROOT / "novel_workspace"))
ctx = build_chapter_context(ws, PID, 1, ch, genre="男频修仙")
print(f"system={len(ctx.system_prompt)}字 user={len(ctx.user_goal)}字 cast={[c.get('name') for c in ctx.cast]}",
      flush=True)

payload = {"model": MODEL, "max_tokens": n, "temperature": 0.7,
           "messages": [{"role": "system", "content": ctx.system_prompt},
                        {"role": "user", "content": ctx.user_goal}]}
req = urllib.request.Request(BASE, data=json.dumps(payload).encode("utf-8"),
                             headers={"Content-Type": "application/json"})
t0 = time.time()
try:
    with urllib.request.urlopen(req, timeout=1800) as r:
        d = json.loads(r.read().decode("utf-8"))
except urllib.error.HTTPError as e:
    body = ""
    try:
        body = e.read().decode("utf-8", "replace")[:400]
    except Exception:  # noqa: BLE001
        pass
    print(f"max_tokens={n}  HTTP {e.code}: {body}")
    sys.exit(1)
except Exception as e:
    print(f"max_tokens={n}  ERROR {type(e).__name__}: {e}")
    sys.exit(1)

m = d["choices"][0]["message"]
det = (d.get("usage") or {}).get("completion_tokens_details") or {}
content = m.get("content") or ""
print(f"max_tokens={n}  正文 {len(content)} 字 | 思考 {len(m.get('reasoning_content') or '')} 字 "
      f"({det.get('reasoning_tokens')} tok) | finish={d['choices'][0].get('finish_reason')} "
      f"| {round(time.time() - t0)}s", flush=True)
if content:
    print(f"  开头 {content[:60]!r}")
    print(f"  结尾 ...{content[-40:]!r}")
else:
    print(f"  思考全文尾部: {(m.get('reasoning_content') or '')[-200:]!r}")
