"""端到端测试：小说场景长输出 + 计时 + reasoning 对比。"""
import os
import paramiko, json, time, sys

HOST, PORT, USER = "connect.bjb2.seetacloud.com", 22214, "root"
PW = os.environ.get("SEETACLOUD_SSH_PW", "")  # 明文凭据不入库，见 _harness/autodl_ssh.txt

PROMPT = (
    "你是一名男频修仙小说作者。请写一段约 300 字的情节正文（不要输出标题、不要思考过程）：\n"
    "主角叶蓝初入青云宗杂役院，夜里被同院老仆何远拉着去后山偷看掌门收徒大典，"
    "结果撞见外门弟子李慕白在欺负新入门的少女云清瑶。"
)

def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, port=PORT, username=USER, password=PW, timeout=30,
                look_for_keys=False, allow_agent=False)

    def chat(effort, max_tokens):
        body = json.dumps({
            "model": "qwen3.6-35b-a3b-fp8",
            "reasoning_effort": effort,
            "messages": [{"role": "user", "content": PROMPT}],
            "max_tokens": max_tokens,
            "temperature": 0.8,
        }, ensure_ascii=False)
        cmd = ("cat > /tmp/e2e.json <<'EOF'\n" + body + "\nEOF\n"
               "curl -s -m 300 -w '\\n===TIME:%{time_total}s===' "
               "-X POST http://127.0.0.1:6006/v1/chat/completions "
               "-H 'Content-Type: application/json' -d @/tmp/e2e.json")
        _, out, _ = ssh.exec_command(cmd, timeout=330)
        return out.read().decode(errors="replace")

    print("===== 测试 A：reasoning_effort=none, max_tokens=800 =====")
    oa = chat("none", 800)
    try:
        j = json.loads(oa.rsplit("===TIME:", 1)[0])
        print("time:", oa.rsplit("===TIME:", 1)[1].replace("===", ""))
        msg = j["choices"][0]["message"]
        print("finish:", j["choices"][0]["finish_reason"])
        print("usage:", j["usage"])
        print("reasoning_len:", len(msg.get("reasoning_content") or ""))
        content = msg.get("content") or ""
        print("content(%d字):" % len(content))
        print(content[:500])
    except Exception as e:
        print("parse err:", e, oa[:300])

    print()
    print("===== 测试 B：reasoning_effort=low(默认思考), max_tokens=800 =====")
    ob = chat("low", 800)
    try:
        j = json.loads(ob.rsplit("===TIME:", 1)[0])
        print("time:", ob.rsplit("===TIME:", 1)[1].replace("===", ""))
        msg = j["choices"][0]["message"]
        print("finish:", j["choices"][0]["finish_reason"])
        print("usage:", j["usage"])
        print("reasoning_len:", len(msg.get("reasoning_content") or ""))
        content = msg.get("content") or ""
        print("content(%d字):" % len(content))
        print(content[:300])
    except Exception as e:
        print("parse err:", e, ob[:300])

    ssh.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
