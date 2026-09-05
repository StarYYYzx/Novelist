# -*- coding: utf-8 -*-
"""B 服务器合并轮询：模型下载完成 + llama.cpp 编译完成。用法: poll_b_ready.py <max_seconds>"""
import paramiko
import sys
import time

from ssh_creds import load as _load_ssh

B = _load_ssh("B")
deadline = time.time() + int(sys.argv[1]) if len(sys.argv) > 1 else time.time() + 2400

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(B["host"], port=B["port"], username="root", password=B["pw"],
            timeout=25, look_for_keys=False, allow_agent=False)

CMD = ("du -sb /root/autodl-tmp/models/Qwen3.8-27B-UD-IQ3_S.gguf 2>/dev/null; "
       "cat /root/autodl-tmp/models/iq3s.done 2>/dev/null || echo DL_NOT_DONE; "
       "tail -1 /root/autodl-tmp/lc-build.log 2>/dev/null | head -c 200; "
       "ls /root/autodl-tmp/lc-build/bin/llama-server 2>/dev/null || echo BIN_MISSING")

while time.time() < deadline:
    try:
        _, out, _ = ssh.exec_command(CMD, timeout=25)
        r = out.read().decode(errors="replace").strip().replace("\n", " || ")
        print("t=%ds %s" % (int(time.time()) % 100000, r), flush=True)
        done = ("DL_OK" in r) and ("llama-server" in r) and ("BIN_MISSING" not in r)
        if done:
            print("BOTH_READY")
            break
    except Exception as ex:
        print("ERR", ex, flush=True)
    time.sleep(30)

ssh.close()
print("POLL_END")
