"""服务器 C 清场：杀 tensorboard + ft/spawn 残留，确认 6006/6007 空闲。"""
import paramiko

from ssh_creds import load as _load_ssh

_C = _load_ssh("C")
HOST, PORT, USER, PW = _C["host"], _C["port"], _C["user"], _C["pw"]
CMDS = [
    "pkill -9 -f tensorboard 2>/dev/null; echo TB_KILLED",
    "ps aux | grep -E 'freetoken|spawn_main|ft serve' | grep -v grep "
    "| awk '{print $2}' | xargs -r kill -9 2>/dev/null; echo FT_KILLED",
    "sleep 1; ss -tlnp | grep -E ':(6006|6007)' || echo PORTS_FREE",
    "ps aux | grep -E 'tensorboard|freetoken|spawn_main' | grep -v grep || echo ALL_CLEAR",
]
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, port=PORT, username=USER, password=PW, timeout=20,
          look_for_keys=False, allow_agent=False)
for cmd in CMDS:
    _, o, e = c.exec_command(cmd, timeout=30)
    out = o.read().decode("utf-8", "replace").strip()
    err = e.read().decode("utf-8", "replace").strip()
    if out:
        print(out)
    if err:
        print("[stderr]", err[:200])
c.close()
