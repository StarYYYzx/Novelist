"""AutoDL 常驻隧道：本地 127.0.0.1:6006 → 容器内 llama-server 6006。

供 20 章测试使用：隧道常驻后，LMStudioProvider(base_url="http://127.0.0.1:6006",
enable_thinking=False) 即可直连云端 9B。后台运行：python tunnel_autodl.py
"""

from __future__ import annotations

import socket
import threading
from pathlib import Path

import paramiko

HERE = Path(__file__).resolve().parent

LOCAL_PORT = 6006
REMOTE = ("127.0.0.1", 6006)


def creds() -> dict:
    d = {}
    for line in (HERE / "autodl_ssh.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            d[k.strip()] = v.strip()
    return d


def main() -> None:
    c = creds()
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(c["SSH_HOST"], port=int(c["SSH_PORT"]), username=c["SSH_USER"],
                password=c["SSH_PASSWORD"], timeout=25)
    transport = ssh.get_transport()
    print(f"隧道建立：本地 {LOCAL_PORT} ← SSH → {REMOTE[0]}:{REMOTE[1]}", flush=True)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", LOCAL_PORT))
    listener.listen(16)

    def handle(conn: socket.socket) -> None:
        try:
            chan = transport.open_channel("direct-tcpip", REMOTE, conn.getpeername())
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

    while True:
        conn, _ = listener.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
