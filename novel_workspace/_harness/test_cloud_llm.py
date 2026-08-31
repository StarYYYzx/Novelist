"""AutoDL 云端 llama-server 测试：SSH direct-tcpip 隧道 + OpenAI 兼容 API 冒烟。

服务器：llama-server 在容器内 6006 端口（/root/models/v3/9B/Qwen3.5-9B-Q8_0.gguf）。
外部访问需经 SSH 隧道——paramiko direct-tcpip channel 直接转发 HTTP，无需本地监听。

用法：python test_cloud_llm.py [prompt]
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

import paramiko

HERE = Path(__file__).resolve().parent


def creds() -> dict:
    d = {}
    for line in (HERE / "autodl_ssh.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            d[k.strip()] = v.strip()
    return d


class Tunnel:
    """SSH direct-tcpip 隧道：把 HTTP 请求经 SSH 转发到容器内 6006。"""

    def __init__(self, host: str, port: int, user: str, password: str,
                 target_host: str = "127.0.0.1", target_port: int = 6006) -> None:
        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh.connect(host, port=port, username=user, password=password, timeout=25)
        self.transport = self.ssh.get_transport()
        self.target = (target_host, target_port)
        print(f"隧道就绪：本地 → SSH({host}:{port}) → {target_host}:{target_port}", flush=True)

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        chan = self.transport.open_channel("direct-tcpip", self.target, ("127.0.0.1", 0))
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body else b""
        headers = (f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:6006\r\n"
                   "Content-Type: application/json\r\n"
                   f"Content-Length: {len(payload)}\r\n"
                   "Connection: close\r\n\r\n")
        chan.send(headers.encode() + payload)
        resp = b""
        while True:
            chunk = chan.recv(65536)
            if not chunk:
                break
            resp += chunk
        chan.close()
        head, _, body_b = resp.partition(b"\r\n\r\n")
        status = int(head.split(b" ")[1])
        text = body_b.decode("utf-8", "replace")
        return {"status": status, "text": text}


def main() -> None:
    c = creds()
    t = Tunnel(c["SSH_HOST"], int(c["SSH_PORT"]), c["SSH_USER"], c["SSH_PASSWORD"])

    print("\n=== /v1/models ===")
    r = t.request("GET", "/v1/models")
    print(f"HTTP {r['status']}", flush=True)
    try:
        for m in json.loads(r["text"]).get("data", []):
            print("  model:", m.get("id"))
    except Exception:
        print(r["text"][:300], flush=True)

    prompt = sys.argv[1] if len(sys.argv) > 1 else "写一句关于剑的句子，不超过20字。"
    print(f"\n=== /v1/chat/completions ===")
    print(f"prompt: {prompt}", flush=True)
    r = t.request("POST", "/v1/chat/completions", {
        "model": "qwen3.5-9b",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 300,
        "temperature": 0.7,
    })
    print(f"HTTP {r['status']}", flush=True)
    if r["status"] != 200:
        print(r["text"][:500], flush=True)
        t.ssh.close()
        return
    d = json.loads(r["text"])
    m = d["choices"][0]["message"]
    print("finish_reason:", d["choices"][0].get("finish_reason"), flush=True)
    print("content:", repr((m.get("content") or "")[:200]), flush=True)
    if m.get("reasoning_content"):
        print("reasoning_content 长度:", len(m["reasoning_content"]), flush=True)
    det = (d.get("usage") or {}).get("completion_tokens_details") or {}
    print("usage:", {k: d["usage"].get(k) for k in ("prompt_tokens", "completion_tokens")},
          "| reasoning_tokens:", det.get("reasoning_tokens"), flush=True)
    t.ssh.close()
    print("\n测试完成", flush=True)


if __name__ == "__main__":
    main()
