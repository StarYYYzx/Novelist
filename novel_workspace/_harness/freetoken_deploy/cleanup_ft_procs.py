"""彻底清理残留的 ft serve / spawn 子进程，按 PID 直杀。"""
import os
import paramiko, sys

HOST, PORT, USER = "connect.bjb2.seetacloud.com", 22214, "root"
PW = os.environ.get("SEETACLOUD_SSH_PW", "")  # 明文凭据不入库，见 _harness/autodl_ssh.txt

def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, port=PORT, username=USER, password=PW, timeout=30,
                look_for_keys=False, allow_agent=False)
    cmds = [
        "ps aux | grep -E 'freetoken|ft serve|spawn_main|scheduler' | grep -v grep | head -8",
        "pkill -9 -f freetoken; pkill -9 -f spawn_main; pkill -9 -f '[f]t serve'; sleep 2; echo KILLED",
        "ps aux | grep -E 'freetoken|ft serve|spawn_main' | grep -v grep || echo PROC_CLEAN",
        "ss -tlnp | grep -E ':6006|:6007' || echo PORT_CLEAN",
        "nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader 2>/dev/null || echo GPU_CLEAN",
    ]
    for c in cmds:
        _, out, err = ssh.exec_command(c, timeout=30)
        print(out.read().decode(errors="replace").strip())
        e = err.read().decode(errors="replace").strip()
        if e:
            print("[err]", e[:300])
    ssh.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
