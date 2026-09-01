"""3080ti 服务器探测：SSH 连接 → 找推理服务进程/端口 → 确认模型文件。

凭据从 autodl_ssh.txt 的 A 组（3080ti）读取。用法：
    python probe_3080ti.py
"""

from __future__ import annotations

from pathlib import Path

import paramiko

HERE = Path(__file__).resolve().parent


def creds_a() -> dict:
    d = {}
    for line in (HERE / "autodl_ssh.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            d[k.strip()] = v.strip()
    return d


def run(ssh: paramiko.SSHClient, cmd: str, timeout: int = 30) -> str:
    _, out, err = ssh.exec_command(cmd, timeout=timeout)
    o = out.read().decode("utf-8", "replace").strip()
    e = err.read().decode("utf-8", "replace").strip()
    return o or e or "(无输出)"


def main() -> None:
    c = creds_a()
    host, port, user, pwd = c["SSH_HOST_A"], int(c["SSH_PORT_A"]), c["SSH_USER_A"], c["SSH_PASSWORD_A"]
    print(f"[1] SSH 连接 {user}@{host}:{port} …", flush=True)
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, port=port, username=user, password=pwd, timeout=25)
    print("    连接成功", flush=True)

    print("[2] 推理服务进程 …", flush=True)
    print(run(ssh, "ps aux | grep -iE 'llama|vllm|ollama|lm.studio|openai|infer' | grep -v grep | head -10"))

    print("[3] 监听端口 …", flush=True)
    print(run(ssh, "ss -tlnp 2>/dev/null | head -20"))

    print("[4] GPU 与模型文件 …", flush=True)
    print(run(ssh, "nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv,noheader 2>/dev/null | head -3"))
    print(run(ssh, "ls -lh /root/autodl-tmp/ /root/models 2>/dev/null; find /root /root/autodl-tmp -maxdepth 3 -name '*.gguf' -o -maxdepth 3 -name '*.safetensors' 2>/dev/null | head -8"))

    print("[5] Python 推理框架 …", flush=True)
    print(run(ssh, "python -c 'import torch; print(\"torch\", torch.__version__, \"cuda\", torch.cuda.is_available())' 2>&1 | head -2"))
    print(run(ssh, "which vllm llama-server ollama 2>/dev/null; vllm --version 2>/dev/null | head -1"))

    ssh.close()
    print("=== 探测完成 ===", flush=True)


if __name__ == "__main__":
    main()
