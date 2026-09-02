"""读取远端 FP8 模型 config.json 关键字段 + index 文件存在性。"""
import os
import paramiko, json, sys

HOST, PORT, USER = "connect.bjb2.seetacloud.com", 22214, "root"
PW = os.environ.get("SEETACLOUD_SSH_PW", "")
DIR = "/root/autodl-tmp/models/Qwen3.6-35B-A3B-FP8"

def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, port=PORT, username=USER, password=PW, timeout=30,
                look_for_keys=False, allow_agent=False)
    _, out, _ = ssh.exec_command("cat %s/config.json 2>/dev/null" % DIR, timeout=30)
    raw = out.read().decode(errors="replace")
    try:
        cfg = json.loads(raw)
    except Exception as e:
        print("config.json 解析失败:", e, raw[:200])
        ssh.close()
        return 1
    keys = ["model_type", "architectures", "num_experts", "num_experts_per_tok",
            "moe_intermediate_size", "hidden_size", "quantization_config", "max_position_embeddings"]
    for k in keys:
        print(k, "=", cfg.get(k))
    qc = cfg.get("quantization_config") or {}
    print("quant_method =", qc.get("quant_method"))
    print("weight_block_size =", qc.get("weight_block_size"))
    _, out, _ = ssh.exec_command("ls %s/ | grep -E 'index|outside|mtp|tokenizer' ; echo ---; ls %s/*.json 2>/dev/null" % (DIR, DIR), timeout=30)
    print(out.read().decode(errors="replace"))
    ssh.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
