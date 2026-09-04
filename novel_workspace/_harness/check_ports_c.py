"""服务器 C 端口占用检查（6006/6007）。"""
import paramiko

from ssh_creds import load as _load_ssh

_C = _load_ssh("C")
HOST, PORT, USER, PW = _C["host"], _C["port"], _C["user"], _C["pw"]
CHECK = (
    "/root/miniconda3/bin/python - <<'EOF'\n"
    "import socket\n"
    "for p in (6006, 6007):\n"
    "    s = socket.socket(); s.settimeout(2)\n"
    "    print(p, 'BUSY' if s.connect_ex(('127.0.0.1', p)) == 0 else 'FREE')\n"
    "    s.close()\n"
    "EOF"
)
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, port=PORT, username=USER, password=PW, timeout=20,
          look_for_keys=False, allow_agent=False)
_, o, e = c.exec_command(CHECK, timeout=30)
print(o.read().decode())
err = e.read().decode().strip()
if err:
    print("[stderr]", err[:300])
c.close()
