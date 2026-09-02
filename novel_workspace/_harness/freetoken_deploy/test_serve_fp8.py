"""验证 FT serve：/v1/models + 一次 chat completion（在远端 curl localhost:6006）。"""
import os
import paramiko, json, sys, time

HOST, PORT, USER = "connect.bjb2.seetacloud.com", 22214, "root"
PW = os.environ.get("SEETACLOUD_SSH_PW", "")

def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, port=PORT, username=USER, password=PW, timeout=30,
                look_for_keys=False, allow_agent=False)

    def run(cmd, timeout=120):
        _, out, err = ssh.exec_command(cmd, timeout=timeout)
        return out.read().decode(errors="replace").strip(), err.read().decode(errors="replace").strip()

    print("== /v1/models ==")
    o, e = run("curl -s -m 20 http://127.0.0.1:6006/v1/models")
    print(o or e)

    body = json.dumps({
        "model": "qwen3.6-35b-a3b-fp8",
        "messages": [{"role": "user", "content": "用一句话介绍修仙小说中的「五五开」系统设定。"}],
        "max_tokens": 200,
        "temperature": 0.7,
    }, ensure_ascii=False)
    # 通过 heredoc 传 JSON 避免引号地狱
    cmd = ("cat > /tmp/chat_req.json <<'EOF'\n%s\nEOF\n"
           "curl -s -m 180 -w '\\n===HTTP%%{http_code}===\\n' "
           "-X POST http://127.0.0.1:6006/v1/chat/completions "
           "-H 'Content-Type: application/json' -d @/tmp/chat_req.json" % body)
    print("== /v1/chat/completions ==")
    o, e = run(cmd, timeout=200)
    # 截断超长输出
    print(o[:4000] if o else e)
    ssh.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
