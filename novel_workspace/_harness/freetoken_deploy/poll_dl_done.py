"""后台轮询：等待 FP8 下载完成（DL_FP8_DONE 标记 + 无 .incomplete 残留）。"""
import os
import paramiko, time, sys

HOST, PORT, USER = "connect.bjb2.seetacloud.com", 22214, "root"
PW = os.environ.get("SEETACLOUD_SSH_PW", "")
DIR = "/root/autodl-tmp/models/Qwen3.6-35B-A3B-FP8"
LOG = "/root/autodl-tmp/installers/dl_fp8.log"
TIMEOUT = int(sys.argv[1]) if len(sys.argv) > 1 else 5400  # 默认 90 分钟

def run(ssh, cmd, timeout=30):
    _, out, err = ssh.exec_command(cmd, timeout=timeout)
    return out.read().decode(errors="replace").strip(), err.read().decode(errors="replace").strip()

def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, port=PORT, username=USER, password=PW, timeout=30,
                look_for_keys=False, allow_agent=False)
    start = time.time()
    last = ""
    while time.time() - start < TIMEOUT:
        o, _ = run(ssh, "tail -2 %s 2>/dev/null; du -sh %s 2>/dev/null; ls %s 2>/dev/null | grep -c incomplete" % (LOG, DIR, DIR), timeout=30)
        txt = o.strip()
        lines = txt.splitlines()
        done = any("DL_FP8_DONE" in l for l in lines)
        inc_count = lines[-1].strip() if lines else "?"
        if txt != last or done:
            print("---- t+%ds ----" % int(time.time() - start))
            print(txt)
            last = txt
        if done and inc_count == "0":
            print("== FP8 DL FULLY DONE ==")
            break
        time.sleep(60)
    else:
        print("== TIMEOUT ==")
    o, _ = run(ssh, "du -sh %s; echo ===; ls -lh %s | grep -c incomplete; echo ===; ls -lh %s | tail -6; echo ===; tail -8 %s" % (DIR, DIR, DIR, LOG), timeout=30)
    print(o)
    ssh.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
