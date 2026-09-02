"""一次性探测：FP8 下载状态 + 残留文件 + 磁盘空间。"""
import os
import paramiko, sys

HOST, PORT, USER = "connect.bjb2.seetacloud.com", 22214, "root"
PW = os.environ.get("SEETACLOUD_SSH_PW", "")
DIR = "/root/autodl-tmp/models/Qwen3.6-35B-A3B-FP8"
LOG = "/root/autodl-tmp/installers/dl_fp8.log"

def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, port=PORT, username=USER, password=PW, timeout=30,
                look_for_keys=False, allow_agent=False)
    cmds = [
        "pgrep -af 'dl_fp8|modelscope' | head -5; echo ---PROC---",
        "tail -5 %s 2>/dev/null; echo ---LOGTAIL---" % LOG,
        "du -sh %s 2>/dev/null; echo ---DU---" % DIR,
        "ls %s 2>/dev/null | wc -l; echo ---FILECOUNT---" % DIR,
        "ls -lh %s 2>/dev/null | grep -c incomplete; echo ---INCOMPLETE_COUNT---" % DIR,
        "ls -lh %s 2>/dev/null | grep incomplete | head -5; echo ---INCOMPLETE_LIST---" % DIR,
        "df -h /root/autodl-tmp | tail -1; echo ---DF---",
        "ls %s/config.json %s/model.safetensors.index.json 2>&1; echo ---CFG---" % (DIR, DIR),
    ]
    for c in cmds:
        _, out, err = ssh.exec_command(c, timeout=40)
        o = out.read().decode(errors="replace").strip()
        e = err.read().decode(errors="replace").strip()
        print(o)
        if e and "No such file" not in e:
            print("[stderr]", e)
    ssh.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
