"""SSH 隧道：本机 127.0.0.1:16012 → 服务器 B 127.0.0.1:6012（llama-server Qwen3.8-27B）。

复用 tunnel_c.py 的 direct-tcpip 转发。常驻进程，Ctrl+C 断开。
"""
import socket
import sys
import threading
import time

import paramiko

from ssh_creds import load as _load_ssh

_B = _load_ssh("B")
HOST, PORT, USER, PW = _B["host"], _B["port"], _B["user"], _B["pw"]
LOCAL_PORT, REMOTE_PORT = 16012, 6012


def _bridge(c1, c2):
    try:
        while True:
            d = c1.recv(65536)
            if not d:
                break
            c2.sendall(d)
    except Exception:
        pass
    finally:
        for c in (c1, c2):
            try:
                c.close()
            except Exception:
                pass


def main():
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", LOCAL_PORT))
    server.listen(16)
    print(f"tunnel_b: 127.0.0.1:{LOCAL_PORT} -> {HOST}:{REMOTE_PORT}", flush=True)

    transport = paramiko.Transport((HOST, PORT))
    transport.connect(username=USER, password=PW)
    print("ssh connected", flush=True)

    def acceptor():
        while True:
            try:
                lc, _ = server.accept()
            except Exception:
                return
            try:
                rc = transport.open_channel(
                    "direct-tcpip", ("127.0.0.1", REMOTE_PORT), ("127.0.0.1", 0))
                threading.Thread(target=_bridge, args=(lc, rc), daemon=True).start()
                threading.Thread(target=_bridge, args=(rc, lc), daemon=True).start()
            except Exception as e:
                print("channel error:", e, flush=True)
                try:
                    lc.close()
                except Exception:
                    pass

    threading.Thread(target=acceptor, daemon=True).start()
    while True:
        time.sleep(60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
