# -*- coding: utf-8 -*-
"""SSH 凭据加载器：从 gitignored 的 autodl_ssh.txt 读取。

明文密码只存在于 _harness/autodl_ssh.txt（已 .gitignore），
任何部署/探测脚本一律通过本模块取凭据，禁止硬编码。
"""
import os


def load(suffix):
    """按服务器后缀（A/B/C）返回 dict(host, port, user, pw)。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "autodl_ssh.txt")
    vals = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip()
    return dict(
        host=vals["SSH_HOST_" + suffix],
        port=int(vals["SSH_PORT_" + suffix]),
        user=vals.get("SSH_USER_" + suffix, "root"),
        pw=vals["SSH_PASSWORD_" + suffix],
    )
