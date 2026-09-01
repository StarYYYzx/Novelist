"""AutoDL 常驻隧道：本地端口 → 容器内 llama-server。

6006 → LLM（Qwen3.5-9B-Q8_0，ctx 16384）
6008 → embedding（nomic-embed-text-v1.5 Q8_0，--embeddings --no-warmup，CPU）

隧道常驻后，本地即可直连：
  LMStudioProvider(base_url="http://127.0.0.1:6006", enable_thinking=False)
  OpenAIEmbedding(model="nomic-embed-text-v1.5", base_url="http://127.0.0.1:6008/v1", api_key="none")
后台运行：python tunnel_autodl.py
"""

from __future__ import annotations

import socket
import threading
from pathlib import Path

import paramiko

HERE = Path(__file__).resolve().parent

# 服务器 A（3080ti，当前主力）：6006 LLM 必建；6008 embedding 未部署（-e 才建）
# 服务器 B（旧 T4，备用）：6006 LLM + 6008 embedding（nomic-embed）
TUNNELS_A = [
    (6006, ("127.0.0.1", 6006)),  # LLM
]
TUNNELS_B = [
    (6006, ("127.0.0.1", 6006)),  # LLM
    (6008, ("127.0.0.1", 6008)),  # embedding
]


def creds(server: str) -> dict:
    d = {}
    for line in (HERE / "autodl_ssh.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            d[k.strip()] = v.strip()
    p = {"A": "SSH_HOST_A", "B": "SSH_HOST_B"}[server]
    return {
        "SSH_HOST": d[p], "SSH_PORT": d[p.replace("HOST", "PORT")],
        "SSH_USER": d[p.replace("HOST", "USER")], "SSH_PASSWORD": d[p.replace("HOST", "PASSWORD")],
    }


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--server", choices=["A", "B"], default="A", help="A=3080ti（默认） B=旧T4")
    ap.add_argument("-e", "--with-embedding", action="store_true",
                    help="额外建 6008 embedding 隧道（仅 B 服务器有 embedding 服务）")
    args = ap.parse_args()
    tunnels = list(TUNNELS_A if args.server == "A" else TUNNELS_B)
    if args.with_embedding:
        tunnels += TUNNELS_B[1:]

    c = creds(args.server)
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(c["SSH_HOST"], port=int(c["SSH_PORT"]), username=c["SSH_USER"],
                password=c["SSH_PASSWORD"], timeout=25,
                look_for_keys=False, allow_agent=False)
    transport = ssh.get_transport()

    listeners: list[tuple[socket.socket, tuple[str, int]]] = []

    def make_listener(local_port: int, remote: tuple[str, int]) -> socket.socket:
        print(f"隧道建立：本地 127.0.0.1:{local_port} ← SSH → {remote[0]}:{remote[1]}", flush=True)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", local_port))
        listener.listen(16)
        return listener

    def handle(conn: socket.socket, remote: tuple[str, int]) -> None:
        try:
            chan = transport.open_channel("direct-tcpip", remote, conn.getpeername())
        except Exception:  # noqa: BLE001
            conn.close()
            return

        def pump(src, dst, srcd, dstd):
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.send(data)
            except Exception:  # noqa: BLE001
                pass
            finally:
                try:
                    if srcd and not srcd.closed:
                        srcd.shutdown(socket.SHUT_WR)
                    if dstd and not dstd.closed:
                        dstd.close()
                except Exception:  # noqa: BLE001
                    pass

        threading.Thread(target=pump, args=(conn, chan, conn, chan), daemon=True).start()
        threading.Thread(target=pump, args=(chan, conn, chan, conn), daemon=True).start()

    for port, remote in tunnels:
        listeners.append((make_listener(port, remote), remote))

    def accept_loop(listener: socket.socket, remote: tuple[str, int]) -> None:
        while True:
            conn, _ = listener.accept()
            threading.Thread(target=handle, args=(conn, remote), daemon=True).start()

    threads = [threading.Thread(target=accept_loop, args=(l, r), daemon=True)
               for l, r in listeners]
    for t in threads:
        t.start()
    print("隧道就绪（Ctrl+C 退出）", flush=True)
    while transport.is_active():
        threading.Event().wait(30)


if __name__ == "__main__":
    main()
