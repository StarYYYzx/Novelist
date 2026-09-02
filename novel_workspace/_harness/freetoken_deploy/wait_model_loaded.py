"""等待模型装载完成（expert banks 31.4G 装载结束）后做 chat completion 测试。"""
import os
import paramiko, json, sys, time

HOST, PORT, USER = "connect.bjb2.seetacloud.com", 22214, "root"
PW = os.environ.get("SEETACLOUD_SSH_PW", "")
LOG = "/root/autodl-tmp/serve_ft.log"
TIMEOUT = int(sys.argv[1]) if len(sys.argv) > 1 else 900

def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, port=PORT, username=USER, password=PW, timeout=30,
                look_for_keys=False, allow_agent=False)
    start = time.time()
    last = ""
    loaded = False
    while time.time() - start < TIMEOUT:
        _, out, _ = ssh.exec_command(
            "curl -s -m 10 -o /dev/null -w '%{http_code}' http://127.0.0.1:6006/v1/models 2>/dev/null; echo; "
            "grep -aE 'Loading experts|expert banks|done|ready|model.*load' " + LOG + " 2>/dev/null | tail -3; "
            "nvidia-smi --query-gpu=memory.used --format=csv,noheader 2>/dev/null", timeout=30)
        txt = out.read().decode(errors="replace").strip()
        lines = txt.splitlines()
        http = lines[0].strip() if lines else ""
        if txt != last:
            print("---- t+%ds (http=%s) ----" % (int(time.time() - start), http))
            print("\n".join(lines[1:]) if len(lines) > 1 else "(无新日志)")
            last = txt
        # 探测：发一次最小请求，200 = 装载完成
        if http == "200":
            _, out2, _ = ssh.exec_command(
                "curl -s -m 15 -X POST http://127.0.0.1:6006/v1/chat/completions "
                "-H 'Content-Type: application/json' "
                "-d '{\"model\":\"qwen3.6-35b-a3b-fp8\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":5}' "
                "-w '\\nHTTP_CODE:%{http_code}' 2>/dev/null | tail -c 500", timeout=30)
            probe = out2.read().decode(errors="replace").strip()
            if "HTTP_CODE:200" in probe:
                loaded = True
                print("== MODEL FULLY LOADED (chat 200) ==")
                break
            if txt != last or "loading" not in probe.lower():
                print("probe:", probe[:200])
        time.sleep(20)
    else:
        print("== TIMEOUT ==")
    if not loaded:
        _, out, _ = ssh.exec_command("tail -30 %s" % LOG, timeout=30)
        print("=====LOGTAIL=====")
        print(out.read().decode(errors="replace"))
    ssh.close()
    return 0 if loaded else 1

if __name__ == "__main__":
    sys.exit(main())
