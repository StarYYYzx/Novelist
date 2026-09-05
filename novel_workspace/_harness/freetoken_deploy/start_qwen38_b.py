# -*- coding: utf-8 -*-
"""B 服务器 Qwen3.8-27B-UD-IQ3_S 启动编排：干净清场 → 启动 → 轮询就绪。
用法: start_qwen38_b.py <max_wait_s>"""
import json
import paramiko
import sys
import time

from ssh_creds import load as _load_ssh

B = _load_ssh("B")
PORT = 6012
LOG = "/root/autodl-tmp/qwen38-serve.log"
MODEL = "/root/autodl-tmp/models/Qwen3.8-27B-UD-IQ3_S.gguf"
BIN = "/root/autodl-tmp/lc-build/bin/llama-server"
max_wait = int(sys.argv[1]) if len(sys.argv) > 1 else 600

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(B["host"], port=B["port"], username="root", password=B["pw"],
            timeout=25, look_for_keys=False, allow_agent=False)


def run(cmd: str, t: int = 60) -> str:
    _, out, err = ssh.exec_command(cmd, timeout=t)
    return out.read().decode(errors="replace") + err.read().decode(errors="replace")


# 1. 清场：按 PID 杀 6012 占用者
run("PID=$(ss -tlnp 2>/dev/null | grep ':%d ' | grep -oP 'pid=\\K[0-9]+' | head -1); "
    "[ -n \"$PID\" ] && kill -9 $PID 2>/dev/null; echo CLEAN" % PORT)
time.sleep(1)

# 2. 启动（新 build llama-server，全层 GPU，q8 KV）
launch = ("LD_LIBRARY_PATH=/root/autodl-tmp/lc-build/lib:$LD_LIBRARY_PATH "
          "setsid nohup %s -m %s --host 0.0.0.0 --port %d "
          "-ngl 99 -c 8192 --cache-type-k q8_0 --cache-type-v q8_0 --jinja "
          "--parallel 1 --log-disable "
          "> %s 2>&1 < /dev/null & echo LAUNCHED_PID=$!" % (BIN, MODEL, PORT, LOG))
print(run(launch, 30))

# 3. 轮询就绪（日志出现 listening / model loaded / HTTP）
deadline = time.time() + max_wait
while time.time() < deadline:
    time.sleep(15)
    r = run("curl -s -m 5 -o /dev/null -w '%%{http_code}' http://127.0.0.1:%d/v1/models 2>/dev/null; echo; "
            "grep -aiE 'model loaded|all slots|listening|error|failed' %s 2>/dev/null | tail -3" % (PORT, LOG), 30)
    code = r.strip().split("\n")[0]
    print("t+%ds http=%s" % (int(deadline - time.time()), code), flush=True)
    if code == "200":
        print("SERVE_READY")
        break
else:
    print("SERVE_TIMEOUT")
    print(run("tail -30 %s" % LOG, 30))

ssh.close()
