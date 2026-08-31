"""AutoDL 云端服务器探测：连接 + 系统信息 + 模型服务扫描。

凭据从 autodl_ssh.txt 读取（不在此文件硬编码密码）。
用法：python probe_autodl.py [--cmd "shell command"]
"""

from __future__ import annotations

import sys
from pathlib import Path

import paramiko

HERE = Path(__file__).resolve().parent


def creds() -> dict:
    d = {}
    for line in (HERE / "autodl_ssh.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            d[k.strip()] = v.strip()
    return d


def run(ssh, cmd: str, timeout: int = 30) -> str:
    _, out, err = ssh.exec_command(cmd, timeout=timeout)
    o = out.read().decode("utf-8", "replace")
    e = err.read().decode("utf-8", "replace")
    return (o + e).strip()


def main() -> None:
    c = creds()
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"连接 {c['SSH_USER']}@{c['SSH_HOST']}:{c['SSH_PORT']} …", flush=True)
    ssh.connect(c["SSH_HOST"], port=int(c["SSH_PORT"]), username=c["SSH_USER"],
                password=c["SSH_PASSWORD"], timeout=25)
    print("连接成功\n", flush=True)

    if len(sys.argv) > 2 and sys.argv[1] == "--cmd":
        print(run(ssh, sys.argv[2]), flush=True)
        ssh.close()
        return

    print("=== 系统 ===")
    print(run(ssh, "uname -a; hostname; nproc"), flush=True)
    print("\n=== GPU ===")
    print(run(ssh, "nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv 2>/dev/null || echo 'no nvidia-smi'"), flush=True)
    print("\n=== 模型服务进程 ===")
    print(run(ssh, "ps aux | grep -iE 'vllm|ollama|lmstudio|text-generation|openai|python.*serve' | grep -v grep || echo '（无匹配进程）'"), flush=True)
    print("\n=== 端口监听 ===")
    print(run(ssh, "ss -tlnp 2>/dev/null | grep -E ':(8000|11434|1234|8080|5000|6006|7860|80)\\s' || echo '（常见端口无监听）'"), flush=True)
    print("\n=== 磁盘/内存 ===")
    print(run(ssh, "free -h | head -3; df -h / /root/autodl-tmp 2>/dev/null | tail -3"), flush=True)
    ssh.close()
    print("\n探测完成", flush=True)


if __name__ == "__main__":
    main()
