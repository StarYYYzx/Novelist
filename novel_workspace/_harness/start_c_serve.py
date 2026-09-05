"""一键拉起服务器 C（Qwen3.6-35B FP8）+ 建立本地 SSH 隧道，全程单脚本：

    本地 LF 规范化上传 → 清场 → 单实例启动 → 真就绪轮询 → SSH 隧道保活

幂等：模型已在跑则跳过清场/启动，直接建隧道；隧道端口被占则视为已建，只验证。
用法：
    python start_c_serve.py [就绪超时秒数，默认1800]

Ctrl+C 退出（进程退出隧道即断；服务器侧 ft serve 仍驻留）。依赖 _harness/ssh_creds.py（读 gitignored autodl_ssh.txt）。
"""
import socket
import sys
import threading
import time
import urllib.request

import paramiko

from ssh_creds import load as _load_ssh

_C = _load_ssh("C")
HOST, PORT, USER, PW = _C["host"], _C["port"], _C["user"], _C["pw"]

LOCAL_SH = "freetoken_deploy/remote_start_fp8.sh"
REMOTE_SH = "/root/autodl-tmp/start_serve.sh"
LOG = "/root/autodl-tmp/serve_ft.log"
REMOTE_PORT, LOCAL_PORT = 6006, 18006
MODEL_ID = "qwen3.6-35b-a3b-fp8"
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


def remote_state(c) -> str:
    """远端模型状态：ready / loading / down。判定含冒烟对话（200 不代表权重已装载）。"""
    probe = (
        f"curl -s -m 10 -o /dev/null -w '%{{http_code}}' "
        f"http://127.0.0.1:{REMOTE_PORT}/v1/models 2>/dev/null; echo; "
        f"curl -s -m 25 http://127.0.0.1:{REMOTE_PORT}/v1/chat/completions "
        "-H 'Content-Type: application/json' -d '{"
        f'"model":"{MODEL_ID}",'
        '"messages":[{"role":"user","content":"ping"}],'
        '"max_tokens":4,'
        '"chat_template_kwargs":{"enable_thinking":false}}\' '
        "| head -c 300")
    o, _ = run(c, probe, 40)
    lines = o.strip().splitlines()
    http = lines[0] if lines else ""
    chat = lines[1] if len(lines) > 1 else ""
    if http != "200":
        return "down"
    if '"error"' in chat:
        return "loading"
    return "ready"


def start_serve(c) -> bool:
    """上传+清场+启动，返回是否已提交启动。"""
    # 1) 本地字节级 LF 规范化（根治 autocrlf 检出 CRLF → 远端启动即死）
    with open(LOCAL_SH, "rb") as f:
        raw = f.read()
    lf = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    if lf != raw:
        with open(LOCAL_SH, "wb") as f:
            f.write(lf)
        log("local: sh 已从 CRLF 规范化为 LF")
    if b"\r" in lf or not lf.startswith(b"#!/bin/bash"):
        log("FATAL: 本地 sh 规范化后异常")
        return False

    # 2) 上传（SFTP 二进制安全）+ 远端首行验证
    sftp = c.open_sftp()
    sftp.put(LOCAL_SH, REMOTE_SH)
    sftp.close()
    o, e = run(c, "sed -n '1p' %s | cat -A" % REMOTE_SH)
    log("uploaded first_line: %s %s" % (o, e[:100]))
    if "^M" in o:
        log("FATAL: 远端脚本仍含 CRLF")
        return False

    # 3) 清场：tensorboard + ft/spawn 残留 + 释放显存
    o, e = run(c,
               "pkill -9 -f tensorboard 2>/dev/null; "
               "ps aux | grep -E 'freetoken|spawn_main|ft serve' | grep -v grep "
               "| awk '{print $2}' | xargs -r kill -9 2>/dev/null; "
               "sleep 1; ss -tlnp | grep -E ':(6006|6007)' || echo PORTS_FREE; "
               "nvidia-smi --query-gpu=memory.used --format=csv,noheader", 30)
    log("clean: %s %s" % (o, e[:100]))

    # 4) 单实例启动（setsid 脱离会话，防 SSH 断开带走进程）
    ch = c.get_transport().open_session()
    ch.exec_command(": > %s; chmod +x %s; "
                    "setsid nohup bash %s > /dev/null 2>&1 < /dev/null & "
                    "echo LAUNCHED_OK" % (LOG, REMOTE_SH, REMOTE_SH))
    time.sleep(3)
    ch.close()
    log("launch submitted (port %d)" % REMOTE_PORT)
    return True


def wait_ready(c) -> bool:
    """轮询远端真就绪；失败时打印日志尾部。"""
    start = time.time()
    last = ""
    while time.time() - start < TIMEOUT:
        st = remote_state(c)
        if st == "ready":
            log("== SERVER READY (t+%ds) ==" % int(time.time() - start))
            return True
        if st == "loading":
            if last != "loading":
                log("api up, weights loading...")
                last = "loading"
        else:
            if last != "down":
                log("port down, waiting for server...")
                last = "down"
        time.sleep(20)
    log("== TIMEOUT ==")
    o, _ = run(c, "tail -80 %s" % LOG, 30)
    log("=====LOG80=====")
    log(o)
    return False


def start_tunnel():
    """direct-tcpip 隧道线程 + 本地探测。返回 (ssh_client 或 None, holder)。"""
    try:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", LOCAL_PORT))
        listener.listen(64)
    except OSError as e:
        if "10048" in str(e) or "in use" in str(e).lower() or "98" in str(e):
            log("local port %d already bound —— 视为隧道已建立，跳过" % LOCAL_PORT)
            return None, [None]
        raise

    def connect():
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(HOST, port=PORT, username=USER, password=PW, timeout=30,
                    look_for_keys=False, allow_agent=False)
        ssh.get_transport().set_keepalive(30)
        return ssh

    ssh = connect()
    holder = [ssh.get_transport()]  # 可变持有：重连后 _pipe 用新 transport

    def _pipe(conn, addr):
        try:
            dest = holder[0].open_channel("direct-tcpip",
                                          ("127.0.0.1", REMOTE_PORT), addr)
        except Exception:
            conn.close()
            return
        conn.settimeout(600)
        dest.settimeout(600)

        def pump(src, dst):
            try:
                while True:
                    data = src.recv(262144)
                    if not data:
                        break
                    dst.sendall(data)
            except Exception:
                pass
            finally:
                for s in (dst, src):
                    try:
                        s.shutdown(2)
                    except Exception:
                        pass
                    try:
                        s.close()
                    except Exception:
                        pass

        threading.Thread(target=pump, args=(conn, dest), daemon=True).start()
        threading.Thread(target=pump, args=(dest, conn), daemon=True).start()

    def serve():
        while True:
            try:
                conn, addr = listener.accept()
            except OSError:
                continue
            threading.Thread(target=_pipe, args=(conn, addr),
                             daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    log("tunnel up: 127.0.0.1:%d -> %s:%d" % (LOCAL_PORT, HOST, REMOTE_PORT))
    return ssh, holder


def tunnel_keepalive(ssh, holder):
    """隧道保活：断线重连并更新 holder，让后续管道走新连接。"""
    while True:
        time.sleep(30)
        if ssh is None:
            return
        if not ssh.get_transport().is_active():
            log("tunnel dead, reconnecting...", flush=True)
            try:
                ssh.close()
            except Exception:
                pass
            try:
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.connect(HOST, port=PORT, username=USER, password=PW,
                            timeout=30, look_for_keys=False, allow_agent=False)
                ssh.get_transport().set_keepalive(30)
                holder[0] = ssh.get_transport()
                log("tunnel re-established")
            except Exception as e:
                log("reconnect failed: %s" % e)


def main() -> int:
    c = conn()

    # ── Phase 1: 远端模型状态判定（幂等：ready 直接跳到隧道）──
    st = remote_state(c)
    log("remote state: %s" % st)
    if st == "down":
        if not start_serve(c):
            c.close()
            return 2
    if st != "ready" and not wait_ready(c):
        c.close()
        return 1

    # ── Phase 2: 冒烟（长输出 + 测速）──
    t0 = time.time()
    smoke = (
        f"curl -s -m 120 http://127.0.0.1:{REMOTE_PORT}/v1/chat/completions "
        "-H 'Content-Type: application/json' -d '{"
        f'"model":"{MODEL_ID}",'
        '"messages":[{"role":"user","content":"一句话介绍金丹期"}],'
        '"max_tokens":96,'
        '"chat_template_kwargs":{"enable_thinking":false}}\' '
        "| head -c 600")
    o, _ = run(c, smoke, 130)
    log("smoke (t=%.1fs): %s" % (time.time() - t0, o[:400]))

    # ── Phase 3: 隧道 + 本地验证 + 保活 ──
    ssh, holder = start_tunnel()
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/v1/models" % LOCAL_PORT, timeout=15) as r:
            log("local probe via tunnel: HTTP %s" % r.status)
    except Exception as e:
        log("local probe via tunnel FAILED: %s" % e)
        c.close()
        return 3

    log("== ALL SET ==  base_url=http://127.0.0.1:%d/v1  model=%s" %
        (LOCAL_PORT, MODEL_ID))
    log("Ctrl+C 退出（进程退出隧道即断；服务器侧 ft serve 仍驻留）")
    c.close()
    try:
        tunnel_keepalive(ssh, holder)
    except KeyboardInterrupt:
        log("bye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
