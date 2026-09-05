"""一键拉起服务器 C（Qwen3.6-35B FP8）：本地 LF 规范化上传 → 清场 → 单实例启动 → 就绪轮询。

整合自 clean_server_c.py + freetoken_deploy/start_serve_final.py + remote_start_fp8.sh，
用法：
    python start_c_serve.py [就绪超时秒数，默认1800]

依赖：_harness/ssh_creds.py（从 gitignored autodl_ssh.txt 读凭据）。
"""
import sys
import time

import paramiko

from ssh_creds import load as _load_ssh

_C = _load_ssh("C")
HOST, PORT, USER, PW = _C["host"], _C["port"], _C["user"], _C["pw"]

LOCAL_SH = "freetoken_deploy/remote_start_fp8.sh"
REMOTE_SH = "/root/autodl-tmp/start_serve.sh"
LOG = "/root/autodl-tmp/serve_ft.log"
TIMEOUT = int(sys.argv[1]) if len(sys.argv) > 1 else 1800


def log(msg: str) -> None:
    print(msg, flush=True)


def conn():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, port=PORT, username=USER, password=PW, timeout=30,
              look_for_keys=False, allow_agent=False)
    return c


def run(c, cmd, timeout=40):
    _, out, err = c.exec_command(cmd, timeout=timeout)
    return (out.read().decode(errors="replace").strip(),
            err.read().decode(errors="replace").strip())


def main() -> int:
    # ── 1) 本地字节级 LF 规范化（根治 autocrlf 检出 CRLF → 远端启动即死）──
    with open(LOCAL_SH, "rb") as f:
        raw = f.read()
    lf = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    if lf != raw:
        with open(LOCAL_SH, "wb") as f:
            f.write(lf)
        log("local: sh 已从 CRLF 规范化为 LF")
    if b"\r" in lf or not lf.startswith(b"#!/bin/bash"):
        log("FATAL: 本地 sh 规范化后异常")
        return 2

    # ── 2) 上传（SFTP 二进制安全）──
    c = conn()
    sftp = c.open_sftp()
    sftp.put(LOCAL_SH, REMOTE_SH)
    sftp.close()
    o, e = run(c, "sed -n '1p' %s | cat -A" % REMOTE_SH)
    log("uploaded first_line: %s %s" % (o, e[:100]))
    if "^M" in o:
        log("FATAL: 远端脚本仍含 CRLF")
        c.close()
        return 2

    # ── 3) 清场：tensorboard + ft/spawn 残留 + 释放显存 ──
    o, e = run(c,
               "pkill -9 -f tensorboard 2>/dev/null; "
               "ps aux | grep -E 'freetoken|spawn_main|ft serve' | grep -v grep "
               "| awk '{print $2}' | xargs -r kill -9 2>/dev/null; "
               "sleep 1; ss -tlnp | grep -E ':(6006|6007)' || echo PORTS_FREE; "
               "nvidia-smi --query-gpu=memory.used --format=csv,noheader", 30)
    log("clean: %s %s" % (o, e[:100]))

    # ── 4) 单实例启动（setsid 脱离会话，防 SSH 断开带走进程）──
    ch = c.get_transport().open_session()
    ch.exec_command(": > %s; chmod +x %s; "
                    "setsid nohup bash %s > /dev/null 2>&1 < /dev/null & "
                    "echo LAUNCHED_OK" % (LOG, REMOTE_SH, REMOTE_SH))
    time.sleep(3)
    ch.close()
    c.close()
    log("launch submitted (port 6006)")

    # ── 5) 就绪轮询：/v1/models 200 + 冒烟对话出内容 = 真就绪 ──
    c2 = conn()
    start = time.time()
    last = ""
    ready = False
    while time.time() - start < TIMEOUT:
        probe = ("curl -s -m 10 -o /dev/null -w '%{http_code}' "
                 "http://127.0.0.1:6006/v1/models 2>/dev/null; echo; "
                 "curl -s -m 25 http://127.0.0.1:6006/v1/chat/completions "
                 "-H 'Content-Type: application/json' -d '{"
                 "\"model\":\"qwen3.6-35b-a3b-fp8\","
                 "\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],"
                 "\"max_tokens\":4,"
                 "\"chat_template_kwargs\":{\"enable_thinking\":false}}' "
                 "| head -c 300; echo; "
                 f"tail -4 {LOG} 2>/dev/null; echo ===GPU===; "
                 "nvidia-smi --query-gpu=memory.used,memory.total "
                 "--format=csv,noheader 2>/dev/null")
        o, _ = run(c2, probe, 40)
        txt = o.strip()
        http = txt.splitlines()[0] if txt else ""
        exited = "FT_SERVE_EXITED" in txt
        if http != "200":
            if txt != last:
                log("---- t+%ds (http=%s) ----" % (int(time.time() - start), http))
                log(txt)
                last = txt
        elif '"error"' in txt:
            # API 层已 200 但权重仍在装载（MoE offload 从内存载入专家）
            log("t+%ds: api up, weights loading..." % int(time.time() - start))
            last = txt
        else:
            ready = True
            log("== SERVER READY (HTTP 200, chat OK) ==")
            break
        if exited:
            log("== SERVER EXITED ==")
            break
        time.sleep(20)
    else:
        log("== TIMEOUT ==")

    if ready:
        # 冒烟：关思考直出一次（验证 chat_template_kwargs 生效）
        o, e = run(c2,
                   "curl -s -m 120 http://127.0.0.1:6006/v1/chat/completions "
                   "-H 'Content-Type: application/json' -d '{"
                   "\"model\":\"qwen3.6-35b-a3b-fp8\","
                   "\"messages\":[{\"role\":\"user\",\"content\":\"一句话介绍金丹期\"}],"
                   "\"max_tokens\":64,\"chat_template_kwargs\":{\"enable_thinking\":false}}' "
                   "| head -c 600", 130)
        log("smoke: %s %s" % (o, e[:100]))
    else:
        o, _ = run(c2, "tail -80 %s" % LOG, 30)
        log("=====LOG80=====")
        log(o)
    c2.close()
    return 0 if ready else 1


if __name__ == "__main__":
    sys.exit(main())
