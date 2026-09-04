"""探测三台 AutoDL 服务器状态：GPU / 端口 / 推理进程。只读不动服务。"""
import sys
sys.path.insert(0, r"E:\360MoveData\Users\Administrator\Desktop\novelist\novel_workspace\_harness")
import paramiko

from ssh_creds import load as _load_ssh

HOSTS = {
    "A-3080ti": _load_ssh("A"),
    "B-T4":     _load_ssh("B"),
    "C-FT35B":  _load_ssh("C"),
}
CMD = (
    "nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader 2>/dev/null; "
    "echo ---PORTS---; ss -tlnp 2>/dev/null | grep -E ':(6006|6007|18006)' ; "
    "echo ---PROC---; ps aux | grep -E 'llama|freetoken|tensorboard' | grep -v grep | awk '{print $11,$12,$13}' | head -5; "
    "echo ---UP---; uptime -p"
)

for name, cfg in HOSTS.items():
    host, port, user, pwd = cfg["host"], cfg["port"], cfg["user"], cfg["pw"]
    print(f"===== {name} {host}:{port} =====", flush=True)
    try:
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(host, port=port, username=user, password=pwd, timeout=12, banner_timeout=12)
        _, o, e = c.exec_command(CMD, timeout=20)
        print(o.read().decode("utf-8", "replace").strip())
        err = e.read().decode("utf-8", "replace").strip()
        if err:
            print("[stderr]", err[:200])
        c.close()
    except Exception as ex:
        print(f"OFFLINE/ERR: {type(ex).__name__}: {ex}")
