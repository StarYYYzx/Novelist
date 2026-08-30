"""探查 LM-Studio 的 qwen3.5 是否把输出放进了 reasoning 字段。

症状：圣经注入后 system prompt 变长，直出返回 0 字符。
怀疑 qwen3.5 是思考型模型，token 预算被 reasoning 吃掉，content 为空。
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

BASE = "http://127.0.0.1:1234/v1/chat/completions"
MODEL = "qwen/qwen3.5-9b"


def call(system: str, user: str, max_tokens: int, label: str) -> None:
    payload = {
        "model": MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        "temperature": 0.7,
    }
    req = urllib.request.Request(BASE, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        data = json.loads(r.read().decode("utf-8"))
    msg = data["choices"][0]["message"]
    usage = data.get("usage", {})
    print(f"--- {label} (max_tokens={max_tokens}, sys={len(system)}字) ---")
    print("   keys:", sorted(msg.keys()))
    print(f"   content 长度: {len(msg.get('content') or '')}")
    for k in ("reasoning_content", "reasoning", "thinking"):
        if msg.get(k):
            print(f"   {k} 长度: {len(msg[k])}  前80字: {msg[k][:80]!r}")
    print(f"   finish_reason: {data['choices'][0].get('finish_reason')}")
    print(f"   usage: {usage}")
    if msg.get("content"):
        print(f"   content 前60字: {msg['content'][:60]!r}")
    print()


def main() -> None:
    short_sys = "你是主编剧。"
    long_sys = ("你是一部男频修仙长篇小说的主编剧，正在写第 1 卷第 1 章。\n\n【世界设定】\n"
                + "\n".join(f"- 铁律：第{i}条规则内容示例，用于把 system prompt 撑长。" for i in range(12))
                + "\n\n【本章出场人物】\n"
                + "\n".join(f"- 人物{i}（男，炼气三层，青云宗）：性格沉稳；弧线：崛起。" for i in range(6))
                + "\n\n【输出纪律】\n- 元叙事：不得出现第X章。\n- 完整性：必须完整收束。\n")

    user = "请撰写第 1 卷第 1 章正文。\n\n细纲：\n苏晚三年一考末位，被铁无涯当众摘去玉牌。\n\n要求：写完要点，结尾完整。"
    call(short_sys, user, 400, "短 system / 400 tokens")
    call(long_sys, user, 400, "长 system / 400 tokens")
    call(long_sys, user, 1400, "长 system / 1400 tokens")
    call(long_sys, user, 2400, "长 system / 2400 tokens")


if __name__ == "__main__":
    main()
