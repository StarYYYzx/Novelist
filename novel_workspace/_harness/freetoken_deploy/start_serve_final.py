"""最终版启动：彻底清场（PID 直杀 spawn 残留）→ 单实例启动 → curl /v1/models 探测真正就绪。"""
import os
import paramiko, sys, time

HOST, PORT, USER = "connect.bjb2.seetacloud.com", 22214, "root"
PW = os.environ.get("SEETACLOUD_SSH_PW", "")  # 明文凭据不入库，见 _harness/autodl_ssh.txt
LOCAL_SH = r"E:/360MoveData/Users/Administrator/Desktop/novelist/novel_workspace/_harness/freetoken_deploy/remote_start_fp8.sh"
REMOTE_SH = "/root/autodl-tmp/start_serve.sh"
LOG = "/root/autodl-tmp/serve_ft.log"
TIMEOUT = int(sys.argv[1]) if len(sys.argv) > 1 else 1800

def conn():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, port=PORT, username=USER, password=PW, timeout=30,
              look_for_keys=False, allow_agent=False)
    return c

def run(c, cmd, timeout=40):
    _, out, err = c.exec_command(cmd, timeout=timeout)
    return out.read().decode(errors="replace").strip(), err.read().decode(errors="replace").strip()

def main():
    # 1) 上传脚本
    c = conn()
    sftp = c.open_sftp()
    sftp.put(LOCAL_SH, REMOTE_SH)
    sftp.close()
    # 2) 清场：杀所有 ft/spawn/python 残留（按 ps 抓 PID 再 kill，避免 pkill 名字自匹配）
    o, e = run(c, "ps aux | grep -E 'freetoken|spawn_main|ft serve' | grep -v grep | awk '{print $2}' | xargs -r kill -9 2>/dev/null; sleep 1; echo CLEAN1")
    print("clean1:", o[:300], e[:200])
    c.close()

    # 3) 单实例启动（独立新连接）
    c = conn()
    ch = c.get_transport().open_session()
    ch.exec_command(": > %s; chmod +x %s; setsid nohup bash %s > /dev/null 2>&1 < /dev/null & echo LAUNCHED_OK" % (LOG, REMOTE_SH, REMOTE_SH))
    time.sleep(3)
    ch.close()
    c.close()
    print("launch submitted")

    # 4) 轮询：HTTP 探测 /v1/models 直到 200（Uvicorn running 只代表 API 层，backend 就绪才算）
    c2 = conn()
    start = time.time()
    last = ""
    ready = False
    while time.time() - start < TIMEOUT:
        o, _ = run(c2, "curl -s -m 10 -o /dev/null -w '%{http_code}' http://127.0.0.1:6006/v1/models 2>/dev/null; echo; "
                       "tail -4 " + LOG + " 2>/dev/null; echo ===GPU===; nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader 2>/dev/null", timeout=30)
        txt = o.strip()
        http = txt.splitlines()[0] if txt else ""
        exited = "FT_SERVE_EXITED" in txt
        if txt != last or http == "200" or exited:
            print("---- t+%ds (http=%s) ----" % (int(time.time() - start), http))
            print(txt)
            last = txt
        if http == "200":
            ready = True
            print("== SERVER READY (HTTP 200) ==")
            break
        if exited:
            print("== SERVER EXITED ==")
            break
        time.sleep(20)
    else:
        print("== TIMEOUT ==")
    if not ready:
        o, _ = run(c2, "tail -80 %s" % LOG, timeout=30)
        print("=====LOG80=====")
        print(o)
    c2.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
