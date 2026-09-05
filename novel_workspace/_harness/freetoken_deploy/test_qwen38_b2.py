# -*- coding: utf-8 -*-
"""第二轮：正确关思考方式的小说场景对比 + 负载实况。"""
import json
import socket
import threading
import time
import urllib.request

import paramiko

from ssh_creds import load as _load_ssh

B = _load_ssh("B")
LOCAL, REMOTE = 16013, 6012
BASE = "http://127.0.0.1:%d/v1" % LOCAL
MODEL = "/root/autodl-tmp/models/Qwen3.8-27B-UD-IQ3_S.gguf"


def _bridge(c1, c2):
    try:
        while True:
            d = c1.recv(65536)
            if not d:
                break
            c2.sendall(d)
    except Exception:
        pass
    finally:
        for c in (c1, c2):
            try:
                c.close()
            except Exception:
                pass


def tunnel_up():
    lsock = socket.socket()
    lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    lsock.bind(("127.0.0.1", LOCAL))
    lsock.listen(8)

    def acceptor():
        while True:
            try:
                lc, _ = lsock.accept()
            except Exception:
                return
            try:
                tr = paramiko.Transport((B["host"], B["port"]))
                tr.connect(username="root", password=B["pw"])
                rc = tr.open_channel("direct-tcpip", ("127.0.0.1", REMOTE), ("127.0.0.1", 0))
                threading.Thread(target=_bridge, args=(lc, rc), daemon=True).start()
                threading.Thread(target=_bridge, args=(rc, lc), daemon=True).start()
            except Exception:
                try:
                    lc.close()
                except Exception:
                    pass

    threading.Thread(target=acceptor, daemon=True).start()
    for _ in range(30):
        try:
            urllib.request.urlopen(BASE + "/models", timeout=5)
            return
        except Exception:
            time.sleep(1)


def chat(body, timeout=400):
    req = urllib.request.Request(BASE + "/chat/completions",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode())
        return d, time.time() - t0
    except Exception as ex:
        return {"error": str(ex)}, time.time() - t0


def gpu():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(B["host"], port=B["port"], username="root", password=B["pw"],
                timeout=20, look_for_keys=False, allow_agent=False)
    _, out, _ = ssh.exec_command(
        "nvidia-smi --query-gpu=memory.used,utilization.gpu --format=csv,noheader", timeout=15)
    r = out.read().decode().strip()
    ssh.close()
    return r


PROMPT = ("写一段约600字的男频修仙小说正文：主角叶蓝与师兄赵铁柱在青云宗外门演武场切磋，"
          "五五开共享系统令两人灵力同涨，引来杂役弟子围观。要求有动作细节、对话、环境烘托，"
          "不要系统提示音之外的说明性文字。")


def run(label, extra):
    body = {"model": MODEL, "messages": [{"role": "user", "content": PROMPT}],
            "max_tokens": 1100, "temperature": 0.9}
    body.update(extra)
    d, wall = chat(body)
    if "error" in d:
        print("[%s] FAIL %s" % (label, d["error"]))
        return
    ch = d["choices"][0]["message"]
    us = d.get("usage") or {}
    n_out = us.get("completion_tokens") or 0
    txt = ch.get("content") or ""
    rc = len(ch.get("reasoning_content") or "")
    print("[%s] wall=%.1fs out=%d tok %.1f tok/s finish=%s len=%d字 reasoning=%d" % (
        label, wall, n_out, n_out / wall if wall else 0,
        d["choices"][0].get("finish_reason"), len(txt), rc))
    print("    gpu=%s" % gpu())
    if rc:
        print("    reasoning_head:", (ch.get("reasoning_content") or "")[:120].replace("\n", " "))
    print("    content_head:", txt[:160].replace("\n", " "))
    print()


if __name__ == "__main__":
    tunnel_up()
    print("model:", MODEL)
    print("== 请求A：思考开启（默认）==")
    run("A_thinking_on", {})
    time.sleep(2)
    print("== 请求B：chat_template_kwargs.enable_thinking=false ==")
    run("B_thinking_off", {"chat_template_kwargs": {"enable_thinking": False}})
    time.sleep(2)
    print("== 请求C：B 后追加一轮多轮对话（测续写稳定性 + 缓存）==")
    body = {"model": MODEL,
            "messages": [{"role": "user", "content": "写一句叶蓝的独白。"}],
            "max_tokens": 200}
    d, wall = chat(body, 200)
    if "error" not in d:
        ch = d["choices"][0]["message"]
        print("[C] wall=%.1fs finish=%s content=%r" % (
            wall, d["choices"][0].get("finish_reason"), (ch.get("content") or "")[:120]))
    else:
        print("[C] FAIL", d["error"])
    print("gpu=%s" % gpu())
