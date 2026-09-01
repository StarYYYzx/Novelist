"""云端模型可用性测试：SSH 隧道 → OpenAI 兼容接口探测。

凭据从 autodl_ssh.txt 读取。用法：
    python probe_cloud_model.py [--gen "测试提示词"]
默认：隧道转发远端 <port> 到本地，测 /v1/models + 一次 chat completion。
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
REMOTE_PORT = 6006     # llama-server 端口（probe_autodl 探测确认）
LOCAL_PORT = 18006     # 本地隧道端口


def creds() -> dict:
    d = {}
    for line in (HERE / "autodl_ssh.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            d[k.strip()] = v.strip()
    return d


def start_tunnel(ssh: paramiko.SSHClient, local_port: int, remote_port: int) -> threading.Thread:
    """本地端口 → SSH direct-tcpip → 远端端口。返回监听线程。"""

    transport = ssh.get_transport()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", local_port))
    listener.listen(16)
    listener.settimeout(1.0)

    def _pipe(conn: socket.socket, addr) -> None:
        try:
            dest = transport.open_channel(
                "direct-tcpip", ("127.0.0.1", remote_port), addr)
        except Exception:
            conn.close()
            return
        conn.settimeout(180)
        dest.settimeout(180)

        def pump(src: socket.socket, dst) -> None:
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
        t1.start()
        t2.start()
        t1.join()
        t2.join()
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
         timeout: float = 120.0) -> tuple[int, dict | str]:
    """极简 HTTP/1.1 client（避免依赖 requests）。"""
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
        if b"\r\n\r\n" in buf and len(buf) > 4096:  # 头已收全，正文按需
            pass
    s.close()
    head, _, body_b = buf.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    try:
        return status, json.loads(body_b.decode("utf-8", "replace"))
    except Exception:
        return status, body_b.decode("utf-8", "replace")[:2000]


def main() -> None:
    c = creds()
    print(f"[1] SSH 连接 {c['SSH_USER']}@{c['SSH_HOST']}:{c['SSH_PORT']} …", flush=True)
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(c["SSH_HOST"], port=int(c["SSH_PORT"]), username=c["SSH_USER"],
                password=c["SSH_PASSWORD"], timeout=25)
    print("    连接成功", flush=True)

    print(f"[2] 隧道 127.0.0.1:{LOCAL_PORT} → 远端 127.0.0.1:{REMOTE_PORT}", flush=True)
    start_tunnel(ssh, LOCAL_PORT, REMOTE_PORT)
    time.sleep(1)

    print("[3] GET /v1/models", flush=True)
    st, data = http(LOCAL_PORT, "GET", "/v1/models")
    print(f"    HTTP {st}", flush=True)
    models = []
    if isinstance(data, dict) and data.get("data"):
        models = [m.get("id", "") for m in data["data"]]
        for m in models:
            print(f"    模型: {m}", flush=True)
    else:
        print(f"    {data}", flush=True)
    if st != 200:
        print("FAIL: /v1/models 不可用", flush=True)
        ssh.close()
        sys.exit(1)

    # 用第一个模型发一次短生成
    model = models[0] if models else "qwen"
    prompt = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--gen" \
        else "用一句话描述一个修仙者突破筑基时的场景。"
    no_think = len(sys.argv) > 2 and sys.argv[1] == "--no-think"
    print(f"[4] POST /v1/chat/completions  model={model}"
          f"{'  (enable_thinking=false)' if no_think else ''}", flush=True)
    print(f"    prompt: {prompt[:60]}…", flush=True)
    body: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 2048,
        "temperature": 0.7,
    }
    if no_think:
        body["chat_template_kwargs"] = {"enable_thinking": False}
    st, data = http(LOCAL_PORT, "POST", "/v1/chat/completions", body)
    print(f"    HTTP {st}", flush=True)
    if st == 200 and isinstance(data, dict):
        choice = data.get("choices", [{}])[0]
        msg = choice.get("message", {})
        content = (msg.get("content") or "").strip()
        usage = data.get("usage", {})
        reason = usage.get("completion_tokens_details", {}).get("reasoning_tokens", "?")
        print(f"    finish_reason={choice.get('finish_reason')}  "
              f"message 字段: {list(msg.keys())}", flush=True)
        if msg.get("reasoning_content"):
            rc = str(msg["reasoning_content"])
            print(f"    思考({len(rc)}字): {rc[:80]}…", flush=True)
        print(f"    正文({len(content)}字): {content[:150]}", flush=True)
        print(f"    usage={json.dumps(usage, ensure_ascii=False)}", flush=True)
        print(f"    tokens: prompt={usage.get('prompt_tokens')} completion={usage.get('completion_tokens')} reasoning={reason}", flush=True)
        ok = len(content) > 0
        print("\n云端推理可用 ✓" if ok else "\n云端推理返回空正文 ✗（思考吃光预算？）", flush=True)
        ssh.close()
        sys.exit(0 if ok else 1)
    else:
        print(f"FAIL: {data}", flush=True)
        ssh.close()
        sys.exit(1)


if __name__ == "__main__":
    main()
