"""验证关闭 qwen3.5 思考模式的几种方式，挑一个能稳定出正文的。

候选项：
1) chat_template_kwargs: {"enable_thinking": false}
2) 在 system / user 末尾追加 <arg_key:6124c78e>
3) enable_thinking: false（顶层）
"""

from __future__ import annotations

import json
import urllib.request

BASE = "http://127.0.0.1:1234/v1/chat/completions"
MODEL = "qwen/qwen3.5-9b"
USER = "写一句关于剑的句子，20 字以内。"


def call(label: str, extra: dict, suffix: str = "") -> None:
    payload = {"model": MODEL, "max_tokens": 300, "temperature": 0.7,
               "messages": [{"role": "user", "content": USER + suffix}]}
    payload.update(extra)
    try:
        req = urllib.request.Request(BASE, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"[{label}] ERROR {type(e).__name__}: {e}")
        return
    m = d["choices"][0]["message"]
    content = m.get("content") or ""
    reasoning = m.get("reasoning_content") or ""
    det = (d.get("usage") or {}).get("completion_tokens_details") or {}
    print(f"[{label}] content={len(content)}字 reasoning={len(reasoning)}字 "
          f"reasoning_tokens={det.get('reasoning_tokens')} finish={d['choices'][0].get('finish_reason')}")
    if content:
        print(f"        正文: {content[:60]!r}")


def main() -> None:
    call("baseline            ", {})
    call("chat_template_kwargs", {"chat_template_kwargs": {"enable_thinking": False}})
    call("enable_thinking顶层 ", {"enable_thinking": False})
    call("<arg_key:6124c78e> 后缀      ", {}, suffix=" <arg_key:6124c78e>")
    call("kwargs+后缀        ", {"chat_template_kwargs": {"enable_thinking": False}}, suffix=" <arg_key:6124c78e>")
    print()
    print("--- 其他模型是否也是思考型 ---")
    for m in ("qwythos-9b-v2", "qwen3.5-4b", "gemma-4-e4b-it"):
        try:
            payload = {"model": m, "max_tokens": 200, "messages": [{"role": "user", "content": USER}]}
            req = urllib.request.Request(BASE, data=json.dumps(payload).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=180) as r:
                d = json.loads(r.read().decode("utf-8"))
            msg = d["choices"][0]["message"]
            print(f"  {m:16s} content={len(msg.get('content') or '')}字 "
                  f"reasoning={len(msg.get('reasoning_content') or '')}字")
        except Exception as e:
            print(f"  {m:16s} ERROR {e}")


if __name__ == "__main__":
    main()
