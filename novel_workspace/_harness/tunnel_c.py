"""SSH 隧道：本机 127.0.0.1:18006 → 服务器 C 127.0.0.1:6006（ft serve）。

复用 probe_cloud_model.py 的 direct-tcpip 转发。常驻进程，Ctrl+C 断开。
"""
import socket
import sys
import threading
import time

import paramiko

from ssh_creds import load as _load_ssh

_C = _load_ssh("C")
HOST, PORT, USER, PW = _C["host"], _C["port"], _C["user"], _C["pw"]
LOCAL_PORT, REMOTE_PORT = 18006, 6006


def start_tunnel(ssh: paramiko.SSHClient, local_port: int, remote_port: int) -> threading.Thread:
    transport = ssh.get_transport()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", local_port))
    listener.listen(64)
    listener.settimeout(1.0)

    def _pipe(conn, addr):
        try:
            dest = transport.open_channel("direct-tcpip", ("127.0.0.1", remote_port), addr)
        except Exception:
            conn.close()
            return
        conn.settimeout(600)
        dest.settimeout(600)

        def pump(src, dst):
            try:
                while True:
                    data = src.recv(262144)
                    if not data:
                        break
                    dst.sendall(data)
            except Exception:
                pass
            finally:
                try:
                    dst.shutdown(2)
                except Exception:
                    pass
                try:
                    src.close()
                except Exception:
                    pass
                try:
                    dst.close()
                except Exception:
                    pass

        t1 = threading.Thread(target=pump, args=(conn, dest), daemon=True)
        t2 = threading.Thread(target=pump, args=(dest, conn), daemon=True)
        t1.start(); t2.start()

    def serve():
        while True:
            try:
                conn, addr = listener.accept()
            except socket.timeout:
                continue
            threading.Thread(target=_pipe, args=(conn, addr), daemon=True).start()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return t


def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, port=PORT, username=USER, password=PW, timeout=30,
                look_for_keys=False, allow_agent=False)
    start_tunnel(ssh, LOCAL_PORT, REMOTE_PORT)
    print(f"tunnel up: 127.0.0.1:{LOCAL_PORT} -> {HOST}:{REMOTE_PORT}", flush=True)
    # 保活：每 60s 发一次 keepalive 包（paramiko 自带 keepalive 更稳）
    ssh.get_transport().set_keepalive(30)
    while True:
        time.sleep(30)
        if not ssh.get_transport().is_active():
            print("tunnel dead, reconnecting...", flush=True)
            try:
                ssh.close()
            except Exception:
                pass
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(HOST, port=PORT, username=USER, password=PW, timeout=30,
                        look_for_keys=False, allow_agent=False)
            ssh.get_transport().set_keepalive(30)
            start_tunnel(ssh, LOCAL_PORT, REMOTE_PORT)
            print("tunnel re-established", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
