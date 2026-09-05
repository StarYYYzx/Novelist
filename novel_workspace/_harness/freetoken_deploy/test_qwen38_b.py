# -*- coding: utf-8 -*-
"""Qwen3.8-27B-UD-IQ3_S 项目相关测试：
1. /v1/models 基本信息  2. thinking 开关行为（默认/关闭对比）
3. token 生成速度（计时+usage）  4. 小说场景长正文生成
用法: test_qwen38_b.py"""
import json
import socket
import threading
import time
import urllib.request

import paramiko

from ssh_creds import load as _load_ssh

B = _load_ssh("B")
LOCAL = 16012
REMOTE = 6012

BASE = "http://127.0.0.1:%d/v1" % LOCAL
MODEL_NAME = "qwen3.8-27b"


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


def tunnel_up() -> None:
    """本地 LOCAL → 远端 REMOTE 的 SSH 隧道（每连接独立转发）。"""
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
    # 连通性探测
    for _ in range(30):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/v1/models" % LOCAL, timeout=5)
            return
        except Exception:
            time.sleep(1)
    print("TUNNEL_PROBE_WARN")


def post(path: str, body: dict, timeout: int = 300) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode()
    wall = time.time() - t0
    return json.loads(raw), wall


def main() -> None:
    # 1. models
    m, _ = post("/models", {})
    for x in m.get("data", []):
        print("MODEL:", x.get("id"), "ctx:", x.get("context_length"))

    # 2. 默认（不传 thinking 参数）——观察是否出 reasoning
    body = {"model": MODEL_NAME, "messages": [{"role": "user", "content": "1+1=?"}],
            "max_tokens": 300, "temperature": 0.7}
    try:
        r, wall = post("/chat/completions", body, 180)
        ch = r["choices"][0]["message"]
        print("\n[默认] wall=%.1fs finish=%s reasoning=%d chars content=%r" % (
            wall, r["choices"][0].get("finish_reason"),
            len(ch.get("reasoning_content") or ""),
            (ch.get("content") or "")[:80]))
        print("  usage:", r.get("usage"))
    except Exception as ex:
        print("[默认] FAIL", ex)

    # 3. enable_thinking=false / chat_template_kwargs 关闭思考
    for kw in ({"enable_thinking": False},
               {"chat_template_kwargs": {"enable_thinking": False}},
               {"chat_template_kwargs": {"thinking": False}}):
        body = {"model": MODEL_NAME,
                "messages": [{"role": "user", "content": "用一句话介绍修仙小说设定里的五五开系统。"}],
                "max_tokens": 400, "temperature": 0.7}
        body.update(kw)
        try:
            r, wall = post("/chat/completions", body, 180)
            ch = r["choices"][0]["message"]
            us = r.get("usage") or {}
            n_out = (us.get("completion_tokens") or 0)
            tok_s = n_out / wall if wall > 0 else 0
            print("\n[关闭思考 %s] wall=%.1fs %.1f tok/s finish=%s" % (
                json.dumps(kw, ensure_ascii=False), wall, tok_s,
                r["choices"][0].get("finish_reason")))
            print("  reasoning=%d chars | content=%r" % (
                len(ch.get("reasoning_content") or ""), (ch.get("content") or "")[:100]))
            print("  usage:", us)
        except Exception as ex:
            print("[关闭思考 %s] FAIL %s" % (kw, ex))

    # 4. 小说场景长正文（正文直出 500+ 字）
    body = {"model": MODEL_NAME,
            "messages": [{"role": "user", "content":
                          "写一段约500字的男频修仙小说正文：主角叶蓝穿越到青云宗外门，"
                          "与师兄赵铁柱绑定五五开系统共享修炼。要求有动作、对话、环境描写。"}],
            "max_tokens": 900, "temperature": 0.9}
    body.setdefault("enable_thinking", False)
    try:
        r, wall = post("/chat/completions", body, 400)
        ch = r["choices"][0]["message"]
        us = r.get("usage") or {}
        n_out = us.get("completion_tokens") or 0
        txt = ch.get("content") or ""
        print("\n[小说场景] wall=%.1fs out=%d tok (%.1f tok/s) finish=%s len=%d字" % (
            wall, n_out, n_out / wall if wall else 0,
            r["choices"][0].get("finish_reason"), len(txt)))
        print("  reasoning_chars=%d" % len(ch.get("reasoning_content") or ""))
        print("  ---- 正文预览 ----")
        print(txt[:600])
    except Exception as ex:
        print("[小说场景] FAIL", ex)


if __name__ == "__main__":
    tunnel_up()
    main()
