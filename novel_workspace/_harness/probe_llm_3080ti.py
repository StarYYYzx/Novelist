"""3080ti LLM 链接测试：SSH 隧道 → 测 /v1/models + 思考开关行为。

- [关思考] chat_template_kwargs.enable_thinking=false → 期望正文直出
- [开思考] 默认/显式 true → 期望 reasoning_content 存在、usage 有 reasoning_tokens
凭据 A 组（3080ti）。用法：python probe_llm_3080ti.py
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path

import paramiko

HERE = Path(__file__).resolve().parent
REMOTE_PORT = 6006
LOCAL_PORT = 18006


def creds_a() -> dict:
    d = {}
    for line in (HERE / "autodl_ssh.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            d[k.strip()] = v.strip()
    return d


def start_tunnel(ssh, local_port: int, remote_port: int) -> threading.Thread:
    transport = ssh.get_transport()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", local_port))
    listener.listen(16)
    listener.settimeout(1.0)

    def _pipe(conn, addr) -> None:
        try:
            dest = transport.open_channel("direct-tcpip", ("127.0.0.1", remote_port), addr)
        except Exception:
            conn.close()
            return
        conn.settimeout(300)
        dest.settimeout(300)

        def pump(src, dst) -> None:
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except Exception:
                pass
            finally:
                try:
                    dst.close()
                except Exception:
                    pass

        t1 = threading.Thread(target=pump, args=(conn, dest), daemon=True)
        t2 = threading.Thread(target=pump, args=(dest, conn), daemon=True)
        t1.start(); t2.start(); t1.join(); t2.join()
        try:
            conn.close()
        except Exception:
            pass

    def _serve() -> None:
        while True:
            try:
                conn, addr = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=_pipe, args=(conn, addr), daemon=True).start()

    th = threading.Thread(target=_serve, daemon=True)
    th.start()
    return th


def http(port: int, method: str, path: str, body: dict | None = None,
         timeout: float = 300.0) -> tuple[int, dict | str]:
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    s.settimeout(timeout)
    payload = json.dumps(body, ensure_ascii=False) if body is not None else ""
    req = f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
    req += "Content-Type: application/json\r\n"
    req += f"Content-Length: {len(payload.encode('utf-8'))}\r\n" if payload else ""
    req += "Connection: close\r\n\r\n" + payload
    s.sendall(req.encode("utf-8"))
    buf = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    head, _, body_b = buf.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    try:
        return status, json.loads(body_b.decode("utf-8", "replace"))
    except Exception:
        return status, body_b.decode("utf-8", "replace")[:2000]


def chat(port: int, prompt: str, max_tokens: int, thinking: bool) -> dict:
    body = {
        "model": "Qwen3.5-9B-Q8_0",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.7,
        "chat_template_kwargs": {"enable_thinking": thinking},
    }
    st, data = http(port, "POST", "/v1/chat/completions", body)
    if st != 200:
        return {"http": st, "err": str(data)[:500]}
    msg = data["choices"][0]["message"]
    u = data.get("usage", {})
    return {
        "http": st,
        "content": (msg.get("content") or "")[:120],
        "reasoning": (msg.get("reasoning_content") or "")[:120],
        "finish": data["choices"][0].get("finish_reason"),
        "tokens": u.get("completion_tokens"),
        "reasoning_tokens": (u.get("completion_tokens_details") or {}).get("reasoning_tokens"),
    }


def main() -> None:
    c = creds_a()
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"[1] SSH {c['SSH_USER_A']}@{c['SSH_HOST_A']}:{c['SSH_PORT_A']}", flush=True)
    ssh.connect(c["SSH_HOST_A"], port=int(c["SSH_PORT_A"]), username=c["SSH_USER_A"],
                password=c["SSH_PASSWORD_A"], timeout=25)
    print("[2] 隧道 18006 → 6006", flush=True)
    start_tunnel(ssh, LOCAL_PORT, REMOTE_PORT)
    time.sleep(1)

    print("[3] GET /v1/models", flush=True)
    st, data = http(LOCAL_PORT, "GET", "/v1/models", timeout=30)
    print(f"    HTTP {st}: {str(data)[:300]}", flush=True)

    print("[4] chat: 关思考 max_tokens=512", flush=True)
    t0 = time.time()
    r1 = chat(LOCAL_PORT, "写一句话：修士在晨雾中练剑。", 512, thinking=False)
    print(f"    {time.time()-t0:.1f}s {json.dumps(r1, ensure_ascii=False)}", flush=True)

    print("[5] chat: 开思考 max_tokens=2048", flush=True)
    t0 = time.time()
    r2 = chat(LOCAL_PORT, "写一句话：修士在晨雾中练剑。", 2048, thinking=True)
    print(f"    {time.time()-t0:.1f}s {json.dumps(r2, ensure_ascii=False)}", flush=True)

    ssh.close()
    print("=== 链接测试完成 ===", flush=True)


if __name__ == "__main__":
    main()
